"""Wayback CDX API client and snapshot selector.

CDX endpoint:
    https://web.archive.org/cdx/search/cdx

Returns every snapshot for a given URL. Large wildcard searches such as
`*.example.com/` may be rejected with HTTP 403, so queries start with
`matchType=exact` and leave BFS discovery to the sitemap module.
"""

from __future__ import annotations

import asyncio
from typing import Iterable
from urllib.parse import quote

import aiohttp

CDX_URL = "https://web.archive.org/cdx/search/cdx"


async def fetch_snapshots(
    url: str,
    *,
    from_ts: str | None = None,
    to_ts: str | None = None,
    match_type: str = "exact",
    limit: int = 200,
    html_only: bool = True,
    session: aiohttp.ClientSession | None = None,
) -> list[tuple[str, str]]:
    """Fetch a list of Wayback snapshots for a URL.

    Args:
        url: Target URL, for example "https://example.com/page.html".
            May contain a `*` wildcard.
        from_ts: Optional start date in "20210101" format.
        to_ts: Optional end date in "20231231" format.
        match_type: "exact" (default), "prefix", or "domain". The "domain"
            mode may return HTTP 403 for large sites.
        limit: Maximum number of snapshots.
        html_only: Restrict results to HTML pages. Disable this for CSS,
            JavaScript, images, fonts, and other assets.
        session: Reusable aiohttp session.

    Returns:
        Ordered list of [(timestamp, original_url), ...] tuples.
    """
    params = {
        "url": url,
        "matchType": match_type,
        "fl": "timestamp,original,statuscode,mimetype",
        "output": "json",
        "limit": str(limit),
        "filter": ["statuscode:200"],
    }
    if html_only:
        params["filter"].append("mimetype:text/html")
    if from_ts:
        params["from"] = from_ts
    if to_ts:
        params["to"] = to_ts

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            headers={"User-Agent": "WaybackDownloader/1.0"},
        )

    try:
        # Preserve repeated filter parameters instead of flattening the dict.
        async with session.get(CDX_URL, params=_build_params(params)) as resp:
            if resp.status == 403:
                raise PermissionError(
                    f"CDX query rejected (403): {url!r} — this is probably "
                    f"a large wildcard search. Try 'exact' mode."
                )
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"CDX error: HTTP {resp.status}: {text[:300]}")
            data = await resp.json(content_type=None)
    finally:
        if own_session:
            await session.close()

    if not data or len(data) < 2:
        return []

    # The first row is the header; the remaining rows contain data.
    header = data[0]
    rows = data[1:]
    ts_idx = header.index("timestamp")
    url_idx = header.index("original")

    out = []
    for row in rows:
        if len(row) > ts_idx and len(row) > url_idx:
            out.append((row[ts_idx], row[url_idx]))
    return out


def _build_params(params: dict) -> list[tuple[str, str]]:
    """Pass repeated filter= values to aiohttp correctly."""
    out = []
    for k, v in params.items():
        if isinstance(v, list):
            for item in v:
                out.append((k, str(item)))
        else:
            out.append((k, str(v)))
    return out


async def fetch_all_unique_pages(
    seed_url: str,
    *,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit_per_query: int = 50,
) -> list[str]:
    """Fetch unique pages across all snapshots of one seed URL.

    Retrieve only text/html snapshots and deduplicate their original URLs,
    including paths.
    """
    snaps = await fetch_snapshots(
        seed_url,
        from_ts=from_ts,
        to_ts=to_ts,
        match_type="exact",
        limit=limit_per_query,
    )
    seen = set()
    for _ts, orig in snaps:
        seen.add(orig)
    return list(seen)


# Raw (id_) snapshot URL format used for downloads.
def raw_snapshot_url(timestamp: str, original_url: str) -> str:
    """Build a raw snapshot URL with the `id_` flag and no toolbar rewriting."""
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"


def raw_snapshot_url_no_ts(original_url: str) -> str:
    """Build a URL without a timestamp so Wayback selects the closest snapshot.

    Use this for fetching any available snapshot, not for browser display.
    """
    return f"https://web.archive.org/web/id_/{original_url}"
