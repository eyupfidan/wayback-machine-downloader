"""BFS discovery and sitemap generation.

The main pipeline uses `discover_pages` to:
1. Query the seed URL through CDX
2. Download it and extract its <a href> links
3. Normalize links on the same origin and add them to the queue
4. Prevent repeated visits with a visited set
5. Enforce the `max_pages` limit

`write_sitemap` writes the downloaded pages to JSON and plain-text files.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional
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
    skipped: list[str] = field(default_factory=list)
    origin_host: str = ""


async def discover_pages_bfs(
    start_urls: list[str],
    *,
    fetcher: WaybackFetcher,
    max_pages: int = 200,
) -> BFSResult:
    """Discover pages on the origin host using BFS.

    Args:
        start_urls: Root URLs, usually a single URL.
        fetcher: Active fetcher.
        max_pages: Maximum number of pages.

    Returns:
        BFSResult containing pages, skipped URLs, and the origin host.
    """
    visited: set[str] = set()
    queue: deque[str] = deque()
    pages: list[PageInfo] = []
    skipped: list[str] = []

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
        log.info("BFS: %s (%d/%d)", url, len(pages) + 1, max_pages)

        # Retrieve snapshot timestamps from CDX.
        try:
            snaps = await fetch_snapshots(url, session=fetcher._session)
        except Exception as e:
            log.warning("CDX error for %s: %s", url, e)
            skipped.append(url)
            continue

        if not snaps:
            skipped.append(url)
            continue

        # Prefer the latest snapshot.
        ts, original = snaps[-1]
        result = await fetcher.fetch_snapshot(original, timestamps=[ts])
        if result.status != 200 or not result.body:
            log.warning("Could not download page: %s (status=%d, err=%s)",
                        url, result.status, result.error)
            skipped.append(url)
            continue

        try:
            html_text = result.body.decode("utf-8", errors="replace")
        except Exception:
            skipped.append(url)
            continue

        # Local path
        from .path_mapper import url_to_local_path
        local = url_to_local_path(original, Path("."))
        local_rel = str(local).replace("\\", "/")

        # Extract internal links.
        new_links = extract_internal_links(html_text, original, origin_host)
        for link in new_links:
            if link not in visited:
                visited.add(link)
                queue.append(link)
            if len(visited) >= max_pages * 3:  # May be visitable, but capped.
                # Do not add more items to the queue.
                pass

        pages.append(PageInfo(
            url=original,
            timestamp=ts,
            local_path=local_rel,
            internal_links=len(new_links),
            status=result.status,
        ))

    return BFSResult(pages=pages, skipped=skipped, origin_host=origin_host)


def write_sitemap(
    pages: list[PageInfo],
    skipped: list[str],
    output_dir: Path,
) -> None:
    """Write sitemap.json and sitemap.txt."""
    sitemap_json = {
        "summary": {
            "pages_downloaded": len(pages),
            "pages_skipped": len(skipped),
        },
        "pages": [asdict(p) for p in pages],
        "skipped": [{"url": u, "reason": "download failed"} for u in skipped],
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
