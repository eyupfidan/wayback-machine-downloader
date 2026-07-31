"""Breadth-first page discovery and sitemap generation.

The main pipeline uses `discover_pages` to:
1. Query the seed URL through CDX
2. Download it and extract its <a href> links
3. Normalize links on the same origin and add them to the queue
4. Prevent repeated visits with a visited set
5. Enforce the `max_pages` limit

`write_sitemap` writes the downloaded pages to JSON and plain-text files.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .cdx import fetch_snapshots
from .fetcher import WaybackFetcher
from .path_mapper import strip_wayback_prefix

log = logging.getLogger(__name__)


@dataclass
class PageInfo:
    """Metadata for a downloaded page."""

    url: str
    timestamp: str
    local_path: str  # Relative to output, e.g. "example.com/about/index.html".
    asset_count: int = 0
    internal_links: int = 0
    status: int = 200
    error: Optional[str] = None
    # The discovery request already downloaded the page. Keep it in memory so
    # the pipeline can discover assets and write the page without two more
    # Wayback requests. This field is intentionally omitted from sitemap.json.
    html_text: str = field(
        default="",
        repr=False,
        compare=False,
        metadata={"sitemap": False},
    )


@dataclass
class SkippedPage:
    """A URL omitted during discovery and the reason it was omitted."""

    url: str
    reason: str


def normalize_url(url: str) -> str:
    """Normalize a URL for comparison.

    - Remove the fragment
    - Sort query parameters so ordering does not create duplicate pages
    - Lowercase scheme + netloc
    - Preserve trailing slashes; use "/" when the path is empty
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    # Sort query parameters.
    qs = parsed.query
    if qs:
        from urllib.parse import parse_qsl, urlencode
        pairs = sorted(parse_qsl(qs, keep_blank_values=True))
        qs = urlencode(pairs)
    return parsed._replace(scheme=scheme, netloc=netloc, fragment="", query=qs).geturl()


def is_internal(url: str, origin_host: str) -> bool:
    """Return whether the URL belongs to the same host."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return False  # A relative path requires a base URL.
    target = parsed.netloc.lower().lstrip("www.")
    existing = origin_host.lower().lstrip("www.")
    return target == existing


def extract_internal_links(html_text: str, current_page_url: str, origin_host: str) -> list[str]:
    """Extract internal <a href> links from HTML."""
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:
        soup = BeautifulSoup(html_text, "html.parser")

    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href or href.startswith("#") or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        # Resolve relative links against the current page URL.
        full = urljoin(current_page_url, href)
        # Strip any Wayback prefix.
        full = strip_wayback_prefix(full)
        if is_internal(full, origin_host) and full.rstrip("/") != current_page_url.rstrip("/"):
            out.append(normalize_url(full))
    # Deduplicate.
    seen = set()
    return [u for u in out if not (u in seen or seen.add(u))]


@dataclass
class BFSResult:
    """Result of BFS discovery."""

    pages: list[PageInfo] = field(default_factory=list)
    skipped: list[SkippedPage] = field(default_factory=list)
    origin_host: str = ""


# Only routes that strongly imply repeatable content are grouped. Ordinary
# pages such as /about and /contact keep their own template keys.
_DETAIL_ROUTE_SEGMENTS = {
    "blog", "blogs", "post", "posts", "article", "articles",
    "news", "haber", "haberler", "yazi", "yazilar",
}
_TAXONOMY_ROUTE_SEGMENTS = {
    "category", "categories", "kategori", "kategoriler", "tag", "tags",
    "etiket", "etiketler",
}


def content_template_key(url: str) -> str | None:
    """Return a grouping key for repeatable content URLs.

    Examples:
        /blog/first-post       -> /blog/:detail
        /category/python       -> /category/:taxonomy
        /blog/category/python  -> /blog/category/:taxonomy

    A normal route such as /about returns None and is never grouped.
    """
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    lowered = [s.lower() for s in segments]

    # A taxonomy marker is more specific than a preceding blog marker.
    for index in range(len(lowered) - 1, -1, -1):
        if lowered[index] in _TAXONOMY_ROUTE_SEGMENTS and index + 1 < len(segments):
            prefix = lowered[:index + 1]
            return f"{parsed.netloc.lower()}/{'/'.join(prefix)}/:taxonomy"

    for index, segment in enumerate(lowered):
        if segment in _DETAIL_ROUTE_SEGMENTS and index + 1 < len(segments):
            prefix = lowered[:index + 1]
            return f"{parsed.netloc.lower()}/{'/'.join(prefix)}/:detail"

    return None


async def discover_pages_bfs(
    start_urls: list[str],
    *,
    fetcher: WaybackFetcher,
    max_pages: int = 200,
    max_per_template: int = 1,
    capture_dir: Path | None = None,
    on_page: Callable[[PageInfo], Awaitable[None]] | None = None,
    preferred_timestamp: str | None = None,
) -> BFSResult:
    """Discover pages on the origin host using BFS.

    Args:
        start_urls: Root URLs, usually a single URL.
        fetcher: Active fetcher.
        max_pages: Maximum number of pages.
        max_per_template: Maximum pages for known repeatable URL templates.
            Set to 0 to disable template grouping.
        capture_dir: When provided, save each page as soon as it is discovered.
            The pipeline later overwrites it with locally rewritten HTML.
        on_page: Optional async hook called after a page is captured and its
            links are queued, before discovery continues to the next page.
        preferred_timestamp: Timestamp supplied in the original Wayback URL.
            When set, use it directly instead of querying CDX for every page.

    Returns:
        BFSResult containing pages, skipped URLs, and the origin host.
    """
    visited: set[str] = set()
    queue: deque[str] = deque()
    pages: list[PageInfo] = []
    skipped: list[SkippedPage] = []
    skipped_urls: set[str] = set()
    template_counts: dict[str, int] = {}

    # Derive the origin host from the first URL.
    if not start_urls:
        return BFSResult()
    origin_host = urlparse(start_urls[0]).netloc

    for u in start_urls:
        norm = normalize_url(u)
        if norm not in visited:
            visited.add(norm)
            queue.append(norm)

    while queue and len(pages) < max_pages:
        url = queue.popleft()
        current_template_key = content_template_key(url)
        if (
            max_per_template > 0
            and current_template_key
            and template_counts.get(current_template_key, 0) >= max_per_template
        ):
            if url not in skipped_urls:
                skipped_urls.add(url)
                skipped.append(SkippedPage(
                    url, f"template limit reached ({current_template_key})"
                ))
            continue

        log.info("BFS: %s (%d/%d)", url, len(pages) + 1, max_pages)

        if preferred_timestamp:
            ts, original = preferred_timestamp, url
        else:
            try:
                snaps = await fetch_snapshots(
                    url,
                    session=fetcher._session,
                    proxy=fetcher.active_proxy,
                )
            except Exception as e:
                log.warning("CDX error for %s: %s", url, e)
                skipped.append(SkippedPage(url, f"CDX error: {e}"))
                continue

            if not snaps:
                skipped.append(SkippedPage(url, "no archived snapshot"))
                continue

            # Prefer the latest snapshot.
            ts, original = snaps[-1]
        result = await fetcher.fetch_snapshot(original, timestamps=[ts])
        if result.status != 200 or not result.body:
            log.warning("Could not download page: %s (status=%d, err=%s)",
                        url, result.status, result.error)
            skipped.append(SkippedPage(
                url, f"download failed (status={result.status}, error={result.error})"
            ))
            continue

        try:
            html_text = result.body.decode("utf-8", errors="replace")
        except Exception:
            skipped.append(SkippedPage(url, "HTML decode failed"))
            continue

        # Local path
        from .path_mapper import url_to_local_path
        local = url_to_local_path(original, Path("."))
        local_rel = str(local).replace("\\", "/")
        if capture_dir is not None:
            capture_path = capture_dir / local_rel
            try:
                capture_path.parent.mkdir(parents=True, exist_ok=True)
                capture_path.write_text(html_text, encoding="utf-8")
            except OSError as e:
                log.warning("Could not save captured page %s: %s", capture_path, e)

        # Count only successfully captured pages. If the first candidate for a
        # template fails, a later candidate can still become its representative.
        if current_template_key:
            template_counts[current_template_key] = (
                template_counts.get(current_template_key, 0) + 1
            )

        # Extract internal links.
        new_links = extract_internal_links(html_text, original, origin_host)
        for link in new_links:
            if link in visited:
                continue

            template_key = content_template_key(link)
            if (
                max_per_template > 0
                and template_key
                and template_counts.get(template_key, 0) >= max_per_template
            ):
                visited.add(link)
                if link not in skipped_urls:
                    skipped_urls.add(link)
                    skipped.append(SkippedPage(
                        link, f"template limit reached ({template_key})"
                    ))
                continue

            # Avoid allowing a very link-heavy page to grow the queue without
            # bound while still permitting failed URLs within max_pages.
            if len(visited) >= max_pages * 3:
                continue

            visited.add(link)
            queue.append(link)

        page = PageInfo(
            url=original,
            timestamp=ts,
            local_path=local_rel,
            internal_links=len(new_links),
            status=result.status,
            html_text=html_text,
        )
        pages.append(page)
        if on_page is not None:
            try:
                await on_page(page)
            except Exception as e:
                log.warning("Per-page processing failed for %s: %s", page.url, e)
                page.error = str(e)

    return BFSResult(pages=pages, skipped=skipped, origin_host=origin_host)


def write_sitemap(
    pages: list[PageInfo],
    skipped: list[SkippedPage],
    output_dir: Path,
) -> None:
    """Write sitemap.json and sitemap.txt."""
    sitemap_json = {
        "summary": {
            "pages_downloaded": len(pages),
            "pages_skipped": len(skipped),
        },
        "pages": [
            {
                item.name: getattr(page, item.name)
                for item in fields(page)
                if item.metadata.get("sitemap", True)
            }
            for page in pages
        ],
        "skipped": [asdict(item) for item in skipped],
    }

    json_path = output_dir / "sitemap.json"
    json_path.write_text(
        json.dumps(sitemap_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    txt_lines = [p.local_path for p in pages]
    txt_path = output_dir / "sitemap.txt"
    txt_path.write_text("\n".join(txt_lines), encoding="utf-8")

    log.info(
        "Sitemap written: %d pages, %d skipped → %s, %s",
        len(pages), len(skipped), json_path, txt_path,
    )
