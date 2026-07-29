"""HTML/CSS/JS rewriting from Wayback URLs to local paths.

The main `rewrite_html` function:
- Visits every <a>, <link>, <script>, <img>, <source>, <iframe>, and srcset
  reference in a BeautifulSoup tree
- Strips the Wayback prefix (`/web/{ts}flag_/`) from each URL
- Converts URLs found in asset_map to relative paths
- Queues unmapped internal URLs for the caller to download
- Leaves external URLs unchanged

`rewrite_inline_style` fixes url() references inside inline <style> elements.

`rewrite_js_basic` scans JavaScript strings used by `fetch(...)`,
`XMLHttpRequest`, `axios.*`, `.src = "..."`, and `.href = "..."`.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment

from .css_processor import URL_PATTERN as CSS_URL_PATTERN
from .path_mapper import (
    NON_DOWNLOADABLE,
    strip_wayback_prefix,
)

log = logging.getLogger(__name__)


# Tags and attributes whose URL values are processed.
URL_ATTRS: list[tuple[str, str]] = [
    ("a", "href"),
    ("link", "href"),
    ("script", "src"),
    ("img", "src"),
    ("source", "src"),
    ("video", "src"),
    ("audio", "src"),
    ("iframe", "src"),
    ("track", "src"),
    ("embed", "src"),
    ("form", "action"),
    ("area", "href"),
    ("object", "data"),
    ("svg", "data"),
    ("use", "href"),
]


# srcset source separator: "url 1x, url 2x" or "url 480w, url 800w".
SRCSET_PATTERN = re.compile(r"(\S+)(\s+\d+[wx])?(?:\s*,)?\s*")
SRCSET_PART = re.compile(r"^\s*(\S+)(?:\s+(\d+[wx]))?\s*$")


def rewrite_html(
    soup: BeautifulSoup,
    *,
    asset_map: dict[str, str],
    enqueue: Optional[Callable[[str], None]] = None,
    page_rel_path: str = "index.html",
    page_origin: str = "",
) -> dict[str, int]:
    """Localize every Wayback URL in a BeautifulSoup tree.

    Args:
        soup: Soup to mutate in place.
        asset_map: Mapping of original URLs to POSIX paths relative to output,
            for example `example.com/style.css`. Treated as immutable.
        enqueue: Called for internal URLs that need downloading. When None,
            their attributes remain unchanged.
        page_rel_path: This page's path relative to output, for example
            `example.com/about/index.html`. HTML paths are calculated relative
            to this value.
        page_origin: Original page URL used to resolve root-relative and
            relative paths, for example
            `https://example.com/about/`.

    Returns:
        Counter dict: {processed: int, skipped: int, queued: int}
    """
    counts = {"processed": 0, "skipped": 0, "queued": 0}

    # Update <base href> first.
    _rewrite_base_href(soup, asset_map)

    for tag_name, attr_name in URL_ATTRS:
        for tag in soup.find_all(tag_name):
            if not tag.has_attr(attr_name):
                continue
            old = tag[attr_name]
            if not old:
                continue
            new = _rewrite_url(old, asset_map, enqueue,
                               source_kind=tag_name,
                               page_rel_path=page_rel_path,
                               page_origin=page_origin)
            if new != old:
                tag[attr_name] = new
                counts["processed"] += 1
            elif strip_wayback_prefix(old) != old:
                counts["skipped"] += 1

    # srcset
    for tag_name in ("img", "source", "picture"):
        for tag in soup.find_all(tag_name):
            if not tag.has_attr("srcset"):
                continue
            old_srcset = tag["srcset"]
            new_srcset = _rewrite_srcset(old_srcset, asset_map, enqueue,
                                         page_rel_path, page_origin)
            if new_srcset != old_srcset:
                tag["srcset"] = new_srcset
                counts["processed"] += 1

    # Inline <style>
    for tag in soup.find_all("style"):
        text = tag.get_text() or ""
        new_text = _rewrite_inline_css_urls(text, asset_map, enqueue,
                                            page_rel_path, page_origin)
        if new_text != text:
            tag.string = new_text

    return counts


def _rewrite_base_href(soup: BeautifulSoup, asset_map: dict[str, str]) -> None:
    """Fix <base href> so it does not misdirect offline navigation."""
    base_tag = soup.find("base")
    if base_tag and base_tag.get("href"):
        original = strip_wayback_prefix(base_tag["href"])
        # A relative base already points within the current directory.
        if original and not original.startswith(("http://", "https://", "//")):
            return  # Already relative.
        # Remove the base and retain default relative-path behavior.
        base_tag.decompose()


def _rewrite_url(
    url: str,
    asset_map: dict[str, str],
    enqueue: Optional[Callable[[str], None]],
    *,
    source_kind: str = "",
    page_rel_path: str = "index.html",
    page_origin: str = "",
) -> str:
    """Rewrite one URL string.

    Values in asset_map are POSIX paths relative to output. Convert them to
    paths relative to page_rel_path before writing them.

    Three URL forms are supported:
    - Absolute (http://... or //host/...): strip the Wayback prefix and convert
      mapped values to relative paths
    - Root-relative (/path): combine the origin and path, then process it
    - Relative (foo/bar): combine the page URL and path

    Mapped URLs become page-relative paths. Unmapped internal URLs are queued,
    while external URLs remain unchanged.
    """
    if not url:
        return url
    if url.startswith("#"):
        return url  # anchor

    stripped = strip_wayback_prefix(url)
    parsed = urlparse(stripped)

    # Leave non-downloadable schemes such as mailto and javascript unchanged.
    if stripped.startswith(NON_DOWNLOADABLE):
        return url
    if parsed.scheme and parsed.scheme not in ("http", "https") and not stripped.startswith("//"):
        return url

    # Resolve relative paths against the original page URL.
    if not parsed.scheme and not stripped.startswith("//") and not stripped.startswith("/web/"):
        if not page_origin:
            return url
        # Combine page_origin and stripped into an absolute URL.
        from urllib.parse import urljoin
        absolute = urljoin(page_origin, stripped)
        stripped = absolute
        parsed = urlparse(stripped)

    # Make root-relative paths absolute with the origin host.
    if (not parsed.scheme or not parsed.netloc) and stripped.startswith("/") and not stripped.startswith("//"):
        if not page_origin:
            return url
        # https://host + /path.
        op = urlparse(page_origin)
        base = f"{op.scheme or 'https'}://{op.netloc}"
        stripped = base + stripped
        parsed = urlparse(stripped)

    # Add https: to scheme-relative URLs.
    if stripped.startswith("//"):
        stripped = "https:" + stripped
        parsed = urlparse(stripped)

    if not parsed.scheme:
        return url

    # Check whether the absolute URL is in asset_map.
    normalized = _normalize_for_asset_map(stripped)
    if normalized in asset_map:
        target_in_output = asset_map[normalized]  # POSIX path relative to output.
        from .path_mapper import relpath_in_output
        return relpath_in_output(page_rel_path, target_in_output)

    # Queue unmapped internal URLs. A host is internal when it matches a host
    # in asset_map, or page_origin when asset_map is empty.
    if parsed.netloc:
        if _is_internal_host(parsed.netloc, asset_map, page_origin):
            if enqueue:
                enqueue(stripped)
            return url  # Leave the original URL for now.

    # Leave URLs on other domains unchanged.
    return url


def _rewrite_srcset(
    srcset: str,
    asset_map: dict[str, str],
    enqueue: Optional[Callable[[str], None]],
    page_rel_path: str = "index.html",
    page_origin: str = "",
) -> str:
    """Split a srcset attribute and rewrite each URL."""
    parts = []
    for entry in srcset.split(","):
        entry = entry.strip()
        if not entry:
            continue
        m = SRCSET_PART.match(entry)
        if not m:
            parts.append(entry)
            continue
        url, desc = m.group(1), m.group(2) or ""
        new = _rewrite_url(url, asset_map, enqueue,
                           source_kind="srcset",
                           page_rel_path=page_rel_path,
                           page_origin=page_origin)
        if desc:
            parts.append(f"{new} {desc}")
        else:
            parts.append(new)
    return ", ".join(parts)


def _rewrite_inline_css_urls(
    css: str,
    asset_map: dict[str, str],
    enqueue: Optional[Callable[[str], None]],
    page_rel_path: str = "index.html",
    page_origin: str = "",
) -> str:
    """Rewrite url(...) references inside an inline <style> element."""

    def repl(m: re.Match) -> str:
        url = m.group(1).strip("\"'")
        new = _rewrite_url(url, asset_map, enqueue,
                           source_kind="inline-css",
                           page_rel_path=page_rel_path,
                           page_origin=page_origin)
        return f'url("{new}")'

    return CSS_URL_PATTERN.sub(repl, css)


def _normalize_for_asset_map(url: str) -> str:
    """Normalize a URL to the asset_map key format.

    Map key: scheme://netloc/path (fragment strip, trailing slash normalize)
    """
    # Add HTTPS when the URL is scheme-relative.
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    # fragment strip
    cleaned = parsed._replace(fragment="").geturl()
    return cleaned


def _is_internal_host(netloc: str, asset_map: dict[str, str], page_origin: str = "") -> bool:
    """Return whether the given host is internal.

    Compare against hosts in asset_map, or page_origin when the map is empty.
    """
    target = netloc.lower().lstrip("www.")
    if not target:
        return True

    if asset_map:
        for key in asset_map:
            kp = urlparse(key)
            if not kp.netloc:
                continue
            existing = kp.netloc.lower().lstrip("www.")
            if existing == target:
                return True
        return False

    # Compare with page_origin when asset_map is empty.
    if page_origin:
        op = urlparse(page_origin)
        if op.netloc:
            existing = op.netloc.lower().lstrip("www.")
            return existing == target
    return False


# ---------------------------------------------------------------------------
# JS best-effort rewriting
# ---------------------------------------------------------------------------

JS_URL_PATTERNS = [
    # fetch("...")
    re.compile(r"""\bfetch\s*\(\s*["']([^"']+)["']"""),
    # XMLHttpRequest.open("GET", "...")
    re.compile(r"""\bopen\s*\(\s*["'](?:GET|POST|PUT|DELETE)["']\s*,\s*["']([^"']+)["']"""),
    # new Image().src = "..." or element.src = "..."
    re.compile(r"""\.src\s*=\s*["']([^"']+)["']"""),
    # element.href = "..."
    re.compile(r"""\.href\s*=\s*["']([^"']+)["']"""),
    # $.load, $.get, $.ajax, axios.get/post
    re.compile(r"""\.(?:load|get|getScript|ajax|getJSON)\s*\(\s*["']([^"']+)["']"""),
    re.compile(r"""\baxios?\.(?:get|post|put|delete|head)\s*\(\s*["']([^"']+)["']"""),
]

# Filter strings that appear in JavaScript but are not URLs.
JS_FALSE_POSITIVE_INDICATORS = re.compile(
    r"\b(function|return|var|let|const|if|else|while|for|case|class|extends|import|export)\b"
    r"|"
    r"^\s*(//|/\*)",
    re.IGNORECASE | re.MULTILINE,
)


def extract_js_urls(js_text: str) -> list[str]:
    """Extract URLs worth downloading from a JavaScript file.

    This is best-effort because bundled JavaScript is difficult to analyze
    reliably. The discovered URLs are intended for the download queue.
    """
    if not js_text:
        return []

    found = []
    seen = set()

    for pat in JS_URL_PATTERNS:
        for m in pat.finditer(js_text):
            url = m.group(1).strip()
            if not url or url in seen:
                continue
            # Filter false positives that look like code strings.
            if url.startswith(("function", "var ", "let ", "return", "if (", "for ", "while ")):
                continue
            seen.add(url)
            found.append(url)

    return found
