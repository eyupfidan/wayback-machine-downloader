"""Orchestrate page discovery, critical resources, deferred assets, and output."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .cleaner import clean_toolbar
from .config import DownloadOptions
from .css_processor import (
    extract_css_import_urls,
    extract_css_urls,
    remove_font_face_block,
    rewrite_css,
)
from .fetcher import BINARY_EXTENSIONS, WaybackFetcher
from .path_mapper import (
    NON_DOWNLOADABLE,
    extract_wayback_timestamp,
    is_non_downloadable,
    strip_wayback_prefix,
    url_to_local_path,
    url_to_local_rel,
)
from .rewriter import (
    extract_js_urls,
    rewrite_html,
)
from .sitemap import (
    PageInfo,
    discover_pages_bfs,
    normalize_url,
    write_sitemap,
)


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

    def __init__(self, args: DownloadOptions) -> None:
        self.args = args
        self.output_dir = Path(args.out).resolve()
        self.preferred_timestamp = extract_wayback_timestamp(args.url)
        self.origin_url = normalize_input_url(args.url)
        self.origin_host = urlparse(self.origin_url).netloc

        # Asset tracking: {original_url: relative_local_path_from_output_dir}
        self.asset_map: dict[str, str] = {}
        # Queue of asset URLs to download.
        self.asset_queue: deque[str] = deque()
        # Images, fonts, media, and dynamic JS resources are downloaded only
        # after BFS has captured every page with its critical CSS and JS.
        self.deferred_asset_queue: deque[str] = deque()
        # Shared assets are attempted only once across the whole crawl.
        self.asset_attempted: set[str] = set()
        self.page_urls: set[str] = set()
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
                on_page=lambda page: self._finish_page(page, fetcher),
                preferred_timestamp=self.preferred_timestamp,
            )
            self.pages = bfs_result.pages
            logging.info("BFS complete: %d pages found, %d skipped",
                         len(self.pages), len(bfs_result.skipped))

            if not self.pages:
                logging.error("No pages found; exiting.")
                return

            # HTML, CSS, and JS are already saved. Download only deferred heavy
            # assets now; pages and critical files are never reopened.
            logging.info(
                "=== Stage 2: Download %d deferred assets ===",
                len(self.deferred_asset_queue),
            )
            await self._download_deferred_assets(fetcher)

            logging.info("=== Stage 3: Write sitemap ===")
            write_sitemap(
                self.pages,
                bfs_result.skipped,
                self.output_dir,
            )

            logging.info("Done. Output: %s", self.output_dir)

    async def _finish_page(
        self,
        page: PageInfo,
        fetcher: WaybackFetcher,
    ) -> None:
        """Save one page with its critical CSS and JS before BFS continues."""
        self.page_urls.add(page.url)
        self.asset_map[page.url] = page.local_path
        self.asset_queue.clear()

        soup = BeautifulSoup(page.html_text, "lxml")
        _enqueue_assets_from_soup(
            soup,
            self.asset_queue,
            self.asset_map,
            page.url,
            deferred_queue=self.deferred_asset_queue,
        )
        logging.info(
            "Saving page with %d critical files (%d assets deferred): %s",
            len(self.asset_queue),
            len(self.deferred_asset_queue),
            page.url,
        )
        await self._download_assets(fetcher, self.asset_queue)
        self._rewrite_and_save_page(page)

        # The page is complete. Releasing its source prevents any later pass
        # from parsing, downloading, or writing this page again.
        page.html_text = ""

    async def _download_deferred_assets(self, fetcher: WaybackFetcher) -> None:
        """Drain heavy assets without reopening any saved HTML/CSS/JS file."""
        while self.asset_queue or self.deferred_asset_queue:
            if self.asset_queue:
                await self._download_assets(fetcher, self.asset_queue)
            if self.deferred_asset_queue:
                await self._download_assets(fetcher, self.deferred_asset_queue)

    async def _download_assets(
        self,
        fetcher: WaybackFetcher,
        queue: deque[str],
    ) -> None:
        """Drain one asset queue in worker-sized recursive batches."""
        while queue:
            batch: list[str] = []
            batch_seen: set[str] = set()
            while queue and len(batch) < fetcher.workers:
                url = strip_wayback_prefix(queue.popleft())
                if not url or url in self.asset_attempted or url in batch_seen:
                    continue
                batch_seen.add(url)
                batch.append(url)

            if batch:
                await asyncio.gather(
                    *(self._download_one_asset(fetcher, url) for url in batch)
                )

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

        if url in self.page_urls or url in self.asset_attempted:
            return
        self.asset_attempted.add(url)

        # POSIX path relative to the output directory.
        rel = self.asset_map.setdefault(url, url_to_local_rel(url))
        # Absolute path on disk.
        local_path = self.output_dir / rel

        # Download.
        try:
            timestamps = (
                [self.preferred_timestamp]
                if self.preferred_timestamp
                else None
            )
            result = await fetcher.fetch_snapshot(
                url,
                timestamps=timestamps,
                allow_fallback=True,
            )
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
                    self._extract_js_urls_to_queue(body_text, url)
                local_path.write_text(body_text, encoding="utf-8")
            else:
                local_path.write_bytes(result.body)
        except Exception as e:
            logging.warning("Could not write asset %s: %s", local_path, e)

    def _rewrite_css_text(self, css_text: str, css_url: str) -> str:
        """Rewrite CSS and recursively discover its referenced URLs."""
        from urllib.parse import urljoin

        urls = extract_css_urls(css_text)
        imports = extract_css_import_urls(css_text)

        for u in urls:
            stripped = strip_wayback_prefix(u)
            if not stripped or is_non_downloadable(stripped):
                continue
            absolute = urljoin(css_url, stripped)
            if urlparse(absolute).scheme not in ("http", "https"):
                continue
            self.asset_map.setdefault(absolute, url_to_local_rel(absolute))
            if u in imports or urlparse(absolute).path.lower().endswith(".css"):
                self.asset_queue.append(absolute)
            else:
                self.deferred_asset_queue.append(absolute)

        # CSS path relative to the output directory.
        css_rel = self.asset_map.get(strip_wayback_prefix(css_url)) or self.asset_map.get(css_url)

        # Rewrite url() references relative to the stylesheet.
        def rewrite_url(url: str) -> str | None:
            stripped = strip_wayback_prefix(url)
            if not stripped or is_non_downloadable(stripped):
                return None
            absolute = urljoin(css_url, stripped)
            target_in_output = self.asset_map.get(absolute)
            if target_in_output and css_rel:
                from .path_mapper import relpath_in_output
                return relpath_in_output(css_rel, target_in_output)
            return stripped

        return rewrite_css(css_text, rewrite_url)

    def _extract_js_urls_to_queue(self, js_text: str, js_url: str) -> None:
        from urllib.parse import urljoin

        urls = extract_js_urls(js_text)
        for u in urls:
            stripped = strip_wayback_prefix(u)
            if not stripped or is_non_downloadable(stripped):
                continue
            absolute = urljoin(js_url, stripped)
            if urlparse(absolute).scheme in ("http", "https"):
                self.deferred_asset_queue.append(absolute)

    def _rewrite_and_save_page(self, page: PageInfo) -> None:
        """Rewrite and save the current page exactly once."""
        soup = BeautifulSoup(page.html_text, "lxml")
        clean_toolbar(soup)
        stats = rewrite_html(
            soup,
            asset_map=self.asset_map,
            enqueue=None,
            page_rel_path=page.local_path,
            page_origin=page.url,
        )

        local_path = self.output_dir / page.local_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(str(soup), encoding="utf-8")
        page.asset_count = stats.get("processed", 0)


def _enqueue_assets_from_soup(
    soup: BeautifulSoup,
    asset_queue: deque,
    asset_map: dict,
    page_url: str,
    *,
    deferred_queue: deque | None = None,
) -> None:
    """Split page resources into critical CSS/JS and deferred heavy assets."""
    from urllib.parse import urljoin

    if deferred_queue is None:
        deferred_queue = asset_queue

    def absolutize(url: str) -> str | None:
        if not url:
            return None
        stripped = strip_wayback_prefix(url)
        if not stripped or is_non_downloadable(stripped):
            return None
        if stripped.startswith(("http://", "https://", "//")):
            return stripped
        if stripped.startswith("/") or not urlparse(stripped).scheme:
            return urljoin(page_url, stripped)
        return stripped

    def enqueue(url: str, *, critical: bool) -> None:
        absolute = absolutize(url)
        if not absolute or is_non_downloadable(absolute):
            return
        asset_map.setdefault(absolute, url_to_local_rel(absolute))
        target = asset_queue if critical else deferred_queue
        target.append(absolute)

    # Stylesheets are critical. Icons, manifests, and preload images/fonts are
    # registered immediately but downloaded after BFS. Canonical/alternate
    # page links are metadata and are not asset downloads.
    for tag in soup.find_all("link", href=True):
        rel_value = tag.get("rel") or []
        if isinstance(rel_value, str):
            rels = {rel_value.lower()}
        else:
            rels = {str(item).lower() for item in rel_value}
        path = urlparse(absolutize(tag["href"]) or "").path.lower()
        if "stylesheet" in rels or path.endswith(".css"):
            enqueue(tag["href"], critical=True)
        elif rels.intersection({"icon", "shortcut", "preload", "manifest"}):
            enqueue(tag["href"], critical=False)

    # External script files are part of the critical page package.
    for tag in soup.find_all("script", src=True):
        enqueue(tag["src"], critical=True)

    # Images and responsive sources are heavy/deferred.
    for tag in soup.find_all(["img", "source"]):
        if tag.get("src"):
            enqueue(tag["src"], critical=False)
        if tag.get("srcset"):
            for entry in tag["srcset"].split(","):
                entry = entry.strip().split()[0]
                if entry:
                    enqueue(entry, critical=False)

    # Media and embedded resources are deferred.
    for tag in soup.find_all(["video", "audio", "iframe", "embed"]):
        src = tag.get("src")
        if src:
            enqueue(src, critical=False)

    # Inline-style imports are critical; images/fonts are deferred.
    for style in soup.find_all("style"):
        text = style.get_text() or ""
        imports = extract_css_import_urls(text)
        urls = extract_css_urls(text)
        for u in urls:
            absolute = absolutize(u)
            critical = (
                u in imports
                or urlparse(absolute or "").path.lower().endswith(".css")
            )
            enqueue(u, critical=critical)


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


