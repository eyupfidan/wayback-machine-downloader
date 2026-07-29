#!/usr/bin/env python3
"""Wayback-to-static-site converter CLI.

Usage:
    python wayback_downloader.py --url "https://web.archive.org/web/20210101/https://example.com/" \\
        --out ./site --workers 8

Pipeline:
1. Normalize the input URL and extract its origin host
2. Discover pages with BFS
3. Download each page's assets (images, CSS, and scripts)
4. Rewrite HTML references to relative paths
5. Generate a sitemap
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from lib.cdx import fetch_snapshots
from lib.cleaner import clean_toolbar
from lib.css_processor import extract_css_urls, remove_font_face_block, rewrite_css
from lib.fetcher import BINARY_EXTENSIONS, WaybackFetcher
from lib.path_mapper import (
    NON_DOWNLOADABLE,
    is_non_downloadable,
    strip_wayback_prefix,
    url_to_local_path,
    url_to_local_rel,
)
from lib.rewriter import (
    extract_js_urls,
    rewrite_html,
)
from lib.sitemap import (
    PageInfo,
    discover_pages_bfs,
    normalize_url,
    write_sitemap,
)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download a static copy of a site from the Wayback Machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Small site
  python wayback_downloader.py \\
      --url "https://web.archive.org/web/20210101/https://example.com/"

  # Date range and output directory
  python wayback_downloader.py \\
      --url "https://example.com/about" \\
      --out ./my-site \\
      --from 20200101 --to 20231231 \\
      --workers 16

  # Increase the page limit
  python wayback_downloader.py \\
      --url "https://example.com" \\
      --max-pages 1000
""",
    )
    p.add_argument("--url", required=True, help="Wayback or origin URL to download")
    p.add_argument("--out", default="./site", help="Output directory (default: ./site)")
    p.add_argument("--workers", type=int, default=8, help="Number of concurrent downloads (default: 8)")
    p.add_argument("--max-pages", type=int, default=200, help="Maximum number of pages to download (default: 200)")
    p.add_argument(
        "--max-per-template",
        type=int,
        default=1,
        help="Maximum pages for repeatable blog/category/tag routes; 0 disables grouping (default: 1)",
    )
    p.add_argument("--from", dest="from_ts", help="Start date (YYYYMMDD)")
    p.add_argument("--to", dest="to_ts", help="End date (YYYYMMDD)")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    return p.parse_args()


def normalize_input_url(url: str) -> str:
    """Normalize a user-supplied URL.

    Accept both `https://example.com/path` and
    `https://web.archive.org/web/{ts}/https://example.com/path`.
    """
    stripped = strip_wayback_prefix(url)
    parsed = urlparse(stripped)
    if parsed.scheme not in ("http", "https"):
        # Assume HTTPS when the user omitted a scheme.
        if not stripped.startswith("//"):
            stripped = "https://" + stripped
    return normalize_url(stripped)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


class Pipeline:
    """Orchestrate the complete download pipeline."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output_dir = Path(args.out).resolve()
        self.origin_url = normalize_input_url(args.url)
        self.origin_host = urlparse(self.origin_url).netloc

        # Asset tracking: {original_url: relative_local_path_from_output_dir}
        self.asset_map: dict[str, str] = {}
        # Queue of asset URLs to download.
        self.asset_queue: deque[str] = deque()
        # Page download state: references to PageInfo objects.
        self.pages: list[PageInfo] = []

    async def run(self) -> None:
        """Run the main pipeline."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logging.info("Target: %s", self.origin_url)
        logging.info("Origin host: %s", self.origin_host)
        logging.info("Output: %s", self.output_dir)

        async with WaybackFetcher(workers=self.args.workers) as fetcher:
            # 1) Page discovery
            logging.info("=== Stage 1: Page discovery (BFS, max=%d) ===", self.args.max_pages)
            bfs_result = await discover_pages_bfs(
                [self.origin_url],
                fetcher=fetcher,
                max_pages=self.args.max_pages,
                max_per_template=self.args.max_per_template,
                capture_dir=self.output_dir,
            )
            self.pages = bfs_result.pages
            # Treat captured pages like any other localized URL so navigation
            # between saved pages is rewritten to relative offline paths.
            for page in self.pages:
                self.asset_map[page.url] = page.local_path
            logging.info("BFS complete: %d pages found, %d skipped",
                         len(self.pages), len(bfs_result.skipped))

            if not self.pages:
                logging.error("No pages found; exiting.")
                return

            # 2) Download pages and discover assets (pass 1)
            logging.info("=== Stage 2: Discover assets from captured pages ===")
            await self._discover_assets_for_pages()

            # 3) Download assets (recursive CSS, best-effort JS, images, and fonts)
            logging.info("=== Stage 3: Download %d assets ===", len(self.asset_queue))
            await self._download_assets(fetcher)

            # 4) Rewrite and save HTML now that asset_map is complete
            logging.info("=== Stage 4: Rewrite HTML and write it to disk ===")
            await self._rewrite_and_save_pages()

            # 5) Generate the sitemap
            logging.info("=== Stage 5: Write sitemap ===")
            write_sitemap(
                self.pages,
                bfs_result.skipped,
                self.output_dir,
            )

            logging.info("Done. Output: %s", self.output_dir)

    async def _discover_assets_for_pages(self) -> None:
        """Discover assets from the HTML retained during BFS."""
        for i, page in enumerate(self.pages):
            logging.info("[%d/%d] Discovering assets: %s", i + 1, len(self.pages), page.url)
            try:
                if not page.html_text:
                    continue
                soup = BeautifulSoup(page.html_text, "lxml")
                _enqueue_assets_from_soup(soup, self.asset_queue, self.asset_map, page.url)

            except Exception as e:
                logging.warning("Asset discovery failed for %s: %s", page.url, e)

    async def _download_assets(self, fetcher: WaybackFetcher) -> None:
        """Download queued assets and process CSS and JavaScript recursively."""
        seen: set[str] = set()
        work_queue: deque[str] = deque()

        for url in self.asset_queue:
            if url not in seen:
                seen.add(url)
                work_queue.append(url)
        # Consume the queue; processing may discover additional URLs.
        while work_queue:
            url = work_queue.popleft()
            await self._download_one_asset(fetcher, url)
            # Process newly queued assets as well.
            while self.asset_queue:
                nu = self.asset_queue.popleft()
                if nu not in seen:
                    seen.add(nu)
                    work_queue.append(nu)

    async def _download_one_asset(self, fetcher: WaybackFetcher, url: str) -> None:
        """Download one asset, save it locally, and recursively process CSS/JS."""
        url = strip_wayback_prefix(url)
        if not url or is_non_downloadable(url):
            return

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") and not url.startswith("//"):
            return  # Relative path without a base URL; skip it.
        if parsed.scheme == "" and not url.startswith("//"):
            return

        if url in self.asset_map:
            return  # Already mapped.

        # POSIX path relative to the output directory.
        rel = url_to_local_rel(url)
        self.asset_map[url] = rel
        # Absolute path on disk.
        local_path = self.output_dir / rel

        # Download.
        try:
            result = await fetcher.fetch_snapshot(url, allow_fallback=True)
        except Exception as e:
            logging.warning("Could not download asset %s: %s", url, e)
            return

        if result.status != 200 or not result.body:
            logging.debug("Skipped asset (status=%d): %s", result.status, url)
            return

        # Corrupted font detection
        is_font = any(url.lower().endswith(ext) for ext in (".woff", ".woff2", ".ttf", ".otf", ".eot"))
        if is_font and result.body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
            logging.info("Detected corrupt font; skipping: %s", url)
            self.asset_map.pop(url, None)
            return

        # Create parent directories.
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            content_type = result.content_type.lower()
            is_text = content_type.startswith(("text/", "application/javascript", "application/json")) or \
                      any(url.lower().endswith(ext) for ext in (
                          ".html", ".css", ".js", ".svg", ".json", ".xml", ".txt", ".map"
                      ))

            if is_text and not is_font:
                body_text = result.body.decode("utf-8", errors="replace")
                # Recursively process CSS @import and url() references.
                if url.lower().endswith(".css") or "css" in content_type:
                    body_text = self._rewrite_css_text(body_text, url)
                # Extract JavaScript URLs on a best-effort basis.
                elif url.lower().endswith((".js", ".mjs")) or "javascript" in content_type:
                    self._extract_js_urls_to_queue(body_text)
                local_path.write_text(body_text, encoding="utf-8")
            else:
                local_path.write_bytes(result.body)
        except Exception as e:
            logging.warning("Could not write asset %s: %s", local_path, e)

    def _rewrite_css_text(self, css_text: str, css_url: str) -> str:
        """Rewrite CSS and recursively discover its referenced URLs."""
        # Extract URLs.
        urls = extract_css_urls(css_text)

        # Add URLs to the queue.
        for u in urls:
            self.asset_queue.append(u)

        # CSS path relative to the output directory.
        css_rel = self.asset_map.get(strip_wayback_prefix(css_url)) or self.asset_map.get(css_url)

        # Rewrite url() references relative to the stylesheet.
        def rewrite_url(url: str) -> str | None:
            stripped = strip_wayback_prefix(url)
            if not stripped or is_non_downloadable(stripped):
                return None
            target_in_output = self.asset_map.get(stripped)
            if target_in_output and css_rel:
                from lib.path_mapper import relpath_in_output
                return relpath_in_output(css_rel, target_in_output)
            # Leave unmapped URLs unchanged for now; a later pass may resolve them.
            return stripped

        return rewrite_css(css_text, rewrite_url)

    def _extract_js_urls_to_queue(self, js_text: str) -> None:
        urls = extract_js_urls(js_text)
        for u in urls:
            self.asset_queue.append(u)

    async def _rewrite_and_save_pages(self) -> None:
        """Rewrite and save the HTML retained during BFS."""
        for i, page in enumerate(self.pages):
            logging.info("[%d/%d] Rewriting and saving HTML: %s → %s",
                         i + 1, len(self.pages), page.url, page.local_path)

            try:
                if not page.html_text:
                    page.error = "HTML was not retained during discovery"
                    continue

                soup = BeautifulSoup(page.html_text, "lxml")

                # Remove the Wayback toolbar.
                clean_toolbar(soup)

                # Rewrite HTML using the page path relative to output.
                stats = rewrite_html(
                    soup,
                    asset_map=self.asset_map,
                    enqueue=lambda u: self.asset_queue.append(u),
                    page_rel_path=page.local_path,
                    page_origin=page.url,
                )

                # Write to disk.
                local_path = self.output_dir / page.local_path
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(str(soup), encoding="utf-8")
                page.asset_count = stats.get("processed", 0)

            except Exception as e:
                logging.warning("HTML rewrite failed for %s: %s", page.url, e)
                page.error = str(e)


def _enqueue_assets_from_soup(
    soup: BeautifulSoup,
    asset_queue: deque,
    asset_map: dict,
    page_url: str,
) -> None:
    """Collect asset URLs from a soup tree (img, link, script, source, etc.).

    URLs are resolved against page_url and queued in absolute form so they can
    be downloaded during Stage 3.
    """
    from urllib.parse import urljoin

    def absolutize(url: str) -> str | None:
        """Make a URL absolute after stripping its Wayback prefix."""
        if not url:
            return None
        stripped = strip_wayback_prefix(url)
        if not stripped or is_non_downloadable(stripped):
            return None
        # URLs with scheme:// or //host are already absolute.
        if stripped.startswith(("http://", "https://", "//")):
            return stripped
        # Otherwise, resolve against page_url.
        if stripped.startswith("/") or not urlparse(stripped).scheme:
            full = urljoin(page_url, stripped)
            return full
        return stripped

    # <link rel="stylesheet" href>, <link rel="icon/preload" href>
    for tag in soup.find_all("link", href=True):
        stripped = absolutize(tag["href"])
        if stripped and stripped not in asset_map and not is_non_downloadable(stripped):
            asset_queue.append(stripped)

    # <script src>
    for tag in soup.find_all("script", src=True):
        stripped = absolutize(tag["src"])
        if stripped and stripped not in asset_map and not is_non_downloadable(stripped):
            asset_queue.append(stripped)

    # <img src>, <source src>
    for tag in soup.find_all(["img", "source"]):
        if tag.get("src"):
            stripped = absolutize(tag["src"])
            if stripped and stripped not in asset_map and not is_non_downloadable(stripped):
                asset_queue.append(stripped)
        # srcset
        if tag.get("srcset"):
            for entry in tag["srcset"].split(","):
                entry = entry.strip().split()[0]
                if entry:
                    stripped = absolutize(entry)
                    if stripped and stripped not in asset_map and not is_non_downloadable(stripped):
                        asset_queue.append(stripped)

    # <video>, <audio>, <iframe>, <embed>
    for tag in soup.find_all(["video", "audio", "iframe", "embed"]):
        src = tag.get("src")
        if src:
            stripped = absolutize(src)
            if stripped and stripped not in asset_map and not is_non_downloadable(stripped):
                asset_queue.append(stripped)

    # url() references inside inline <style> elements.
    for style in soup.find_all("style"):
        text = style.get_text() or ""
        urls = extract_css_urls(text)
        for u in urls:
            stripped = absolutize(u)
            if stripped and stripped not in asset_map and not is_non_downloadable(stripped):
                asset_queue.append(stripped)


def _relative_posix(from_path: Path, to_path: Path) -> str:
    """Return a relative POSIX path from from_path to to_path."""
    import posixpath
    fp = from_path.as_posix().split("/")
    tp = to_path.as_posix().split("/")
    # Common prefix.
    common = 0
    for a, b in zip(fp, tp):
        if a == b:
            common += 1
        else:
            break
    up = [".."] * (len(fp) - common - 1)
    down = tp[common:]
    rel = "/".join(up + down)
    return rel or "."


async def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    try:
        pipeline = Pipeline(args)
        await pipeline.run()
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        return 130
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
