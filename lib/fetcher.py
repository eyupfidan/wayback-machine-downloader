"""Asynchronous concurrent fetcher for downloading files from Wayback.

- aiohttp-based rate limiting with a semaphore
- Exponential backoff for HTTP 429
- Three-tier timestamp fallback for snapshot 404/503 responses, adapted from
  GeiserX/Wayback-Archive
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .cdx import fetch_snapshots, raw_snapshot_url

log = logging.getLogger(__name__)

USER_AGENT = "WaybackDownloader/1.0 (+https://github.com/local/wayback-tool)"

# File types that Wayback should return as binary data.
BINARY_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".ico", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".ogg", ".mp3", ".wav",
    ".zip", ".tar", ".gz", ".br",
)


@dataclass
class FetchResult:
    """Result of one download attempt."""

    url: str
    timestamp: str
    status: int
    body: bytes
    content_type: str = ""
    error: Optional[str] = None


@dataclass
class WaybackFetcher:
    """Central fetcher that manages all downloads.

    Shares one aiohttp.ClientSession, limits concurrency with a semaphore, and
    applies global backoff after HTTP 429 responses.
    """

    workers: int = 8
    timeout_seconds: int = 45
    max_retries: int = 4

    _sem: asyncio.Semaphore = field(init=False)
    _session: Optional[aiohttp.ClientSession] = field(default=None, init=False)
    # Minimum interval between requests, in seconds.
    _min_interval: float = 0.25
    _last_request_time: float = 0.0
    _lock: asyncio.Lock = field(init=False)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.workers)
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0

    async def __aenter__(self) -> "WaybackFetcher":
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            connector=aiohttp.TCPConnector(limit=self.workers * 2),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    async def _rate_limit(self) -> None:
        """Apply the global minimum interval between requests."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_request_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_time = asyncio.get_event_loop().time()

    async def _fetch_with_retry(self, url: str) -> FetchResult:
        """Download one URL with retries and exponential backoff."""
        last_error = None
        for attempt in range(self.max_retries):
            await self._rate_limit()
            assert self._session is not None
            try:
                async with self._session.get(url, allow_redirects=True) as resp:
                    if resp.status == 429:
                        backoff = min(2 ** attempt, 30)
                        log.warning(
                            "Received HTTP 429 (%s); waiting %.1fs (attempt %d/%d)",
                            url, backoff, attempt + 1, self.max_retries,
                        )
                        await asyncio.sleep(backoff)
                        continue

                    if resp.status == 200:
                        body = await resp.read()
                        ct = resp.headers.get("Content-Type", "")
                        return FetchResult(
                            url=url,
                            timestamp="",
                            status=200,
                            body=body,
                            content_type=ct,
                        )

                    # Other 4xx/5xx responses do not need a retry.
                    text = await resp.text()
                    return FetchResult(
                        url=url,
                        timestamp="",
                        status=resp.status,
                        body=text.encode("utf-8", errors="replace"),
                        content_type=resp.headers.get("Content-Type", ""),
                        error=f"HTTP {resp.status}",
                    )

            except asyncio.TimeoutError:
                last_error = "timeout"
                backoff = min(2 ** attempt, 16)
                log.warning("Timeout (%s); retrying in %.1fs", url, backoff)
                await asyncio.sleep(backoff)
            except aiohttp.ClientError as e:
                last_error = str(e)
                backoff = min(2 ** attempt, 16)
                log.warning("ClientError (%s): %s; retrying in %.1fs",
                            url, e, backoff)
                await asyncio.sleep(backoff)

        return FetchResult(
            url=url,
            timestamp="",
            status=0,
            body=b"",
            error=f"maximum retries exceeded: {last_error}",
        )

    async def fetch_snapshot(
        self,
        original_url: str,
        *,
        timestamps: list[str] | None = None,
        allow_fallback: bool = True,
    ) -> FetchResult:
        """Download a specific original URL from Wayback.

        Try `timestamps` in order. If every attempt fails and
        `allow_fallback=True`, use the three-tier fallback.

        Args:
            original_url: Target original URL without a Wayback prefix.
            timestamps: Timestamps to try in YYYYMMDDhhmmss format. Retrieved
                automatically from CDX when omitted.
            allow_fallback: Run the three-tier search when all timestamps
                return 404.

        Returns:
            FetchResult; a status of 200 indicates success.
        """
        if not timestamps:
            snaps = await fetch_snapshots(
                original_url,
                html_only=False,
                session=self._session,
            )
            # CDX returns chronological rows. Prefer recent captures and limit
            # the attempts in fetch_snapshot to the newest five.
            timestamps = [ts for ts, _u in reversed(snaps)]

        # Signatures that distinguish Wayback's "not archived" placeholder
        # HTML, usually a home-page redirect, from real content.
        placeholder_signatures = (
            b"<title>Wayback Machine</title>",
            b"Wayback Machine",
            b"web.archive.org/web/",
        )

        def is_placeholder_response(body: bytes) -> bool:
            """Return whether an HTTP 200 body is Wayback placeholder HTML."""
            head = body[:512].lower()
            return any(sig.lower() in head for sig in placeholder_signatures)

        # HTML returned for a binary or asset URL is a placeholder.
        asset_extensions = (
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
            ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".mp4", ".webm", ".ogg", ".mp3", ".wav",
            ".pdf", ".zip",
            ".css", ".js",  # CSS/JS URLs may also return placeholder HTML.
        )
        is_asset = original_url.lower().endswith(asset_extensions)

        # Try the supplied timestamps first.
        if timestamps:
            for ts in timestamps[:5]:
                url = raw_snapshot_url(ts, original_url)
                result = await self._fetch_with_retry(url)
                result.timestamp = ts
                if result.status == 200:
                    if is_asset and is_placeholder_response(result.body):
                        # This is not real content; skip it.
                        continue
                    return result

        # Try without a timestamp so Wayback selects the closest snapshot.
        no_ts_url = f"https://web.archive.org/web/id_/{original_url}"
        result = await self._fetch_with_retry(no_ts_url)
        if result.status == 200:
            if is_asset and is_placeholder_response(result.body):
                # Unarchived asset.
                pass
            else:
                return result

        # Fallback: retrieve timestamps from a wider CDX range.
        if allow_fallback and timestamps:
            fallback_timestamps = await self._build_fallback_timestamps(original_url, timestamps)
            for layer_label, tss in fallback_timestamps:
                for ts in tss:
                    url = raw_snapshot_url(ts, original_url)
                    result = await self._fetch_with_retry(url)
                    result.timestamp = ts
                    if result.status == 200:
                        if is_asset and is_placeholder_response(result.body):
                            continue
                        log.info("Found snapshot with fallback %s: %s @%s",
                                 layer_label, original_url, ts)
                        return result

        return FetchResult(
            url=original_url, timestamp="", status=0, body=b"",
            error=f"download failed with every method (timestamps={len(timestamps)}, "
                  f"no_ts=attempted, fallback={allow_fallback})",
        )

    async def fetch_direct(self, url: str) -> FetchResult:
        """Download a specific timestamped Wayback URL directly."""
        return await self._fetch_with_retry(url)

    async def _build_fallback_timestamps(
        self,
        original_url: str,
        known_ts: list[str],
    ) -> list[tuple[str, list[str]]]:
        """Build a three-tier timestamp fallback list.

        Tiers, based on GeiserX/Wayback-Archive:
        1. Known timestamp ±12 hours in 2-hour steps
        2. ±48 hours in 6-hour steps
        3. ±168 hours (one week) in 24-hour steps
        """
        from datetime import datetime, timedelta

        if not known_ts:
            return []

        try:
            base = datetime.strptime(known_ts[0], "%Y%m%d%H%M%S")
        except ValueError:
            return []

        out = []
        for hours, step in [(12, 2), (48, 6), (168, 24)]:
            tss = []
            for offset in range(-hours, hours + 1, step):
                if offset == 0:
                    continue
                t = base + timedelta(hours=offset)
                tss.append(t.strftime("%Y%m%d%H%M%S"))
            out.append((f"±{hours}h", tss))
        return out
