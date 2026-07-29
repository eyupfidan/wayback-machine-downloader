"""Wayback toolbar and injected-artifact cleaner.

Wayback injects toolbar scripts, stylesheets, and images into archived HTML.
They are unnecessary in an offline copy and can break URLs by redirecting them
to 404 pages. This module removes:

- Toolbar DOM elements such as #wm-ipp-base and .wm-toolbar
- Wombat.js and bundle-playback.js behavior-modifying scripts
- Wayback stylesheets such as banner-styles.css and iconochive.css
- RufflePlayer content used for Flash emulation
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup


# Toolbar DOM ID and class markers.
TOOLBAR_ID_SUBSTRINGS = ("wm-ipp", "wm-bipp", "wm-toolbar", "donato")
TOOLBAR_CLASS_SUBSTRINGS = ("wm-toolbar", "donato")

# Wayback asset patterns.
SCRIPT_SRC_BAD = (
    "wombat.js",
    "bundle-playback.js",
    "ruffle.js",
    "web.archive.org",
    "archive.org/static",
    "static.archive.org",
    "/_static/",
)

LINK_HREF_BAD = (
    "banner-styles.css",
    "iconochive.css",
    "web.archive.org",
    "/_static/",
)

# Unwanted string signatures inside <script> elements.
SCRIPT_STRING_BAD = (
    "__wm.wombat",
    "Web Archive",
    "RufflePlayer",
    "archive.org/static",
    "__wm.init(",
    "wombatInit",
    "WB_wombat_Init",
    "window.RufflePlayer",
)

TOOLBAR_IFRAME_BAD = ("web.archive.org",)


def clean_toolbar(soup: BeautifulSoup) -> None:
    """Remove Wayback toolbar artifacts from a BeautifulSoup tree.

    Mutates the soup in place.
    """
    _remove_toolbar_elements(soup)
    _remove_bad_scripts(soup)
    _remove_bad_links(soup)
    _remove_bad_inline_scripts(soup)
    _remove_toolbar_iframes(soup)
    _remove_archive_comments(soup)


def _remove_toolbar_elements(soup: BeautifulSoup) -> None:
    """Remove toolbar elements such as #wm-*, .wm-*, and #donato."""
    # Match by ID.
    for el in soup.find_all(True):
        el_id = el.get("id", "") or ""
        for bad in TOOLBAR_ID_SUBSTRINGS:
            if bad in el_id:
                el.decompose()
                break
        else:
            # Match by class.
            el_classes = el.get("class", []) or []
            for cls in el_classes:
                if any(bad in cls for bad in TOOLBAR_CLASS_SUBSTRINGS):
                    el.decompose()
                    break


def _remove_bad_scripts(soup: BeautifulSoup) -> None:
    """Remove <script> elements whose src contains a Wayback pattern."""
    for tag in soup.find_all("script"):
        src = (tag.get("src") or "").lower()
        if any(bad in src for bad in SCRIPT_SRC_BAD):
            tag.decompose()


def _remove_bad_links(soup: BeautifulSoup) -> None:
    """Remove <link> elements whose href contains a Wayback pattern."""
    for tag in soup.find_all("link"):
        href = (tag.get("href") or "").lower()
        if any(bad in href for bad in LINK_HREF_BAD):
            tag.decompose()


def _remove_bad_inline_scripts(soup: BeautifulSoup) -> None:
    """Remove inline scripts containing a Wayback signature."""
    for tag in soup.find_all("script"):
        if tag.get("src"):
            continue  # Already handled by src.
        text = tag.string or ""
        if any(bad in text for bad in SCRIPT_STRING_BAD):
            tag.decompose()


def _remove_toolbar_iframes(soup: BeautifulSoup) -> None:
    """Remove Wayback toolbar iframes."""
    for tag in soup.find_all("iframe"):
        src = (tag.get("src") or "").lower()
        if any(bad in src for bad in TOOLBAR_IFRAME_BAD):
            tag.decompose()


def _remove_archive_comments(soup: BeautifulSoup) -> None:
    """Remove Wayback comments such as <!-- BEGIN WAYBACK TOOLBAR INSERT -->."""
    pattern = re.compile(
        r"^\s*<!--\s*(?:BEGIN|END) WAYBACK",
        re.IGNORECASE,
    )
    from bs4 import Comment

    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if pattern.match(str(c) or ""):
            c.extract()
