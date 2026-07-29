"""CSS dependency extraction and URL rewriting.

Find every asset reference in a stylesheet, including images, fonts, and other
stylesheets, then download and convert them to local paths. Processing has two
stages:

1. `extract_css_urls(css)` — collect URLs from raw CSS
2. `rewrite_css(css, url_map)` — replace those URLs with local paths

This module also handles corrupt fonts. If Wayback returns an HTML 404 page for
a font request, browsers cannot parse it, so the associated @font-face rule is
removed from the CSS.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

log = logging.getLogger(__name__)


# CSS url() pattern supporting quoted and unquoted values.
URL_PATTERN = re.compile(
    r"""
    url\(\s*                                  # url(
    (?P<quote>["']?)                           # optional opening quote
    (?P<url>[^"')]+?)                          # URL without quotes or parentheses
    (?P=quote)                                 # closing quote, when present
    \s*\)                                     # )
    """,
    re.VERBOSE,
)

# @import pattern.
IMPORT_PATTERN = re.compile(
    r"""
    @import\s+                                # @import
    (?:
        url\(\s*["']?(?P<url1>[^"')]+?)["']?\s*\)   # url("...")
        |
        ["'](?P<url2>[^"']+)["']                   # "..."
    )
    \s*[^;]*;?
    """,
    re.VERBOSE | re.IGNORECASE,
)


def extract_css_urls(css_text: str) -> list[str]:
    """Extract every url() and @import URL from a stylesheet."""
    found = []

    # Process @import first because it usually appears near the top.
    for m in IMPORT_PATTERN.finditer(css_text):
        url = m.group("url1") or m.group("url2")
        if url:
            found.append(_clean_css_url(url))

    # url()
    for m in URL_PATTERN.finditer(css_text):
        url = m.group("url")
        if url:
            found.append(_clean_css_url(url))

    # Deduplicate while preserving order.
    seen = set()
    out = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_css_import_urls(css_text: str) -> set[str]:
    """Return URLs referenced specifically by CSS @import rules."""
    imports = set()
    for match in IMPORT_PATTERN.finditer(css_text):
        url = match.group("url1") or match.group("url2")
        if url:
            imports.add(_clean_css_url(url))
    return imports


def rewrite_css(
    css_text: str,
    rewrite_url: Callable[[str], str | None],
) -> str:
    """Rewrite every CSS url() and @import reference with rewrite_url.

    rewrite_url(url) returns a new URL, or None to leave it unchanged:
        - internal URLs return a local path from asset_map
        - external URLs remain unchanged or retain their Wayback form
        - empty or invalid URLs return None
    """

    def url_replace(match: re.Match) -> str:
        quote = match.group("quote") or ""
        url = match.group("url")
        new = rewrite_url(url.strip())
        if new is None:
            return match.group(0)
        return f"url({quote}{new}{quote})"

    css_text = URL_PATTERN.sub(url_replace, css_text)

    def import_replace(match: re.Match) -> str:
        url = match.group("url1") or match.group("url2")
        if not url:
            return match.group(0)
        new = rewrite_url(url.strip())
        if new is None:
            return match.group(0)
        # Preserve unmapped external URLs to avoid breaking the CSS.
        if new == url:
            return match.group(0)
        # The internal reference was rewritten.
        return match.group(0).replace(url, new)

    css_text = IMPORT_PATTERN.sub(import_replace, css_text)
    return css_text


def remove_font_face_block(css_text: str, font_url: str) -> str:
    """Remove the entire @font-face rule containing the given font URL.

    Wayback sometimes returns an HTML 404 page for a font URL. Browsers then
    attempt to parse it as a font and render the page incorrectly.
    """
    # Find @font-face { ... } blocks with simple brace matching.
    pattern = re.compile(r"@font-face\s*\{", re.IGNORECASE)

    out = []
    last_end = 0

    for m in pattern.finditer(css_text):
        start = m.start()
        # Match braces after the opening `{`.
        depth = 1
        i = m.end()
        while i < len(css_text) and depth > 0:
            c = css_text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        end = i

        block = css_text[start:end]
        # Check whether this block contains font_url.
        if font_url in block:
            # Remove it while preserving the preceding content.
            out.append(css_text[last_end:start])
            last_end = end
        else:
            # Keep it.
            pass

    out.append(css_text[last_end:])
    return "".join(out)


def _clean_css_url(url: str) -> str:
    """Remove surrounding whitespace and quotes from a CSS URL."""
    url = url.strip().strip("\"'")
    return url
