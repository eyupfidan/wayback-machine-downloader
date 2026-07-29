"""URL-to-local-path conversion.

Convert original URLs downloaded from Wayback into filesystem-safe paths.
All file operations use pathlib so Unicode characters work correctly on
Windows.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


# Regular expression that strips the Wayback prefix.
# Examples:
#   https://web.archive.org/web/20210101000000id_/https://example.com/page.html
#   https://web.archive.org/web/20210101000000if_/http://example.com/
#   /web/20210101000000cs_/https://example.com/style.css   (relative form)
WAYBACK_PREFIX_RE = re.compile(
    r"""
    ^(?:https?:)?                    # optional scheme
    //web\.archive\.org/web/
    (\d+)(?:[a-z]+_)?/               # timestamp + optional flag (id_, im_, cs_, js_, if_, ...)
    (.*)$                              # remaining original URL
    """,
    re.VERBOSE,
)

# Schemes that cannot be extracted or downloaded.
NON_DOWNLOADABLE = ("tel:", "mailto:", "javascript:", "data:", "ftp:", "sms:", "whatsapp:", "#")


def strip_wayback_prefix(url: str) -> str:
    """Strip the Wayback prefix (`/web/{ts}flag_/`) from a URL.

    Return the input unchanged when no prefix is found. Relative forms such as
    `/web/20210101id_/https://example.com/` are also supported.
    """
    if not url:
        return url
    m = WAYBACK_PREFIX_RE.match(url)
    if m:
        return m.group(2)
    # Some rewritten URLs use this format:
    #   https://web.archive.org/web/20210101000000/https://example.com/...
    m2 = re.match(r"^(?:https?:)?//web\.archive\.org/web/\d+/(.*)$", url)
    if m2:
        return m2.group(1)
    return url


def is_non_downloadable(url: str) -> bool:
    """Return whether a URL uses a non-downloadable scheme."""
    if not url or url.startswith("#") or url.startswith(("tel:", "mailto:", "javascript:", "data:", "ftp:", "sms:", "whatsapp:")):
        return True
    return False


def safe_filename(name: str, max_len: int = 120) -> str:
    """Make a file name ASCII-safe and hash it when it is too long.

    - Normalize Unicode with NFKD and remove non-ASCII characters
    - Replace NTFS-forbidden characters with underscores
    - Remove trailing dots and spaces rejected by Windows
    """
    if not name:
        return "index"

    # URL-decode, for example %20 to a space.
    name = unquote(name)

    # Normalize Unicode.
    name = unicodedata.normalize("NFKD", name)
    # Drop non-ASCII characters; accented Latin letters are transliterated.
    ascii_name = name.encode("ascii", "ignore").decode("ascii")
    if not ascii_name:
        ascii_name = "file"

    # Remove forbidden characters.
    ascii_name = re.sub(r'[<>:"/\\|?*]', "_", ascii_name)
    ascii_name = ascii_name.strip(" .")

    # Truncate and append a hash when the name is too long.
    if len(ascii_name) > max_len:
        h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        ascii_name = ascii_name[: max_len - 9] + "_" + h

    if not ascii_name:
        ascii_name = "file"
    return ascii_name


def url_to_local_rel(original_url: str) -> str:
    """Convert an original URL to a **POSIX path string** relative to output.

    The base directory is omitted. The result is a relative POSIX path such as
    `example.com/about/index.html`, without a leading slash. HTML generation
    later resolves it relative to the page location.

    Rules:
    - `http://host/foo/bar`   → `host/foo/bar/index.html`
    - `http://host/foo/`      → `host/foo/index.html`
    - `http://host/foo`       → `host/foo/index.html`
    - `http://host/foo.html`  → `host/foo.html`
    - Replace query-string punctuation with `_`: `?a=1&b=2` → `_a_1_b_2`
    - Ignore fragments such as `#section`
    """
    parsed = urlparse(original_url)

    if not parsed.netloc and not parsed.path:
        return "index.html"

    netloc = parsed.netloc.lower()
    path = parsed.path or "/"

    # Root URLs map directly to index.html below the host directory.
    if path == "/":
        return netloc + "/index.html"

    # Include the query string in the file name.
    if parsed.query:
        query_safe = re.sub(r"[&=]", "_", parsed.query)
        query_safe = re.sub(r"[^A-Za-z0-9_-]", "_", query_safe)
        if "." in posixpath.basename(path):
            name, ext = posixpath.splitext(posixpath.basename(path))
            path = posixpath.join(posixpath.dirname(path), f"{safe_filename(name, 80)}{ext}_{query_safe}")
        else:
            path = posixpath.join(path, f"_{query_safe}") if path != "/" else f"/_{query_safe}"

    # URL-decode path components before sanitizing their file names.
    path_parts = [safe_filename(part) for part in path.split("/") if part]
    if not path_parts:
        path_parts = ["index"]

    # Determine whether the last component is already a file name.
    last = path_parts[-1]
    if "." not in last:
        # Directory → index.html.
        path_parts.append("index.html")

    path_parts = [p for p in path_parts if p]

    rel = "/".join([netloc] + path_parts)
    return rel


def url_to_local_path(original_url: str, base_dir: Path) -> Path:
    """Convert an original URL to a path on disk.

    Wraps url_to_local_rel. That function always returns POSIX separators,
    including on Windows, which is important for browser paths.
    """
    rel = url_to_local_rel(original_url)
    # Join the path components portably.
    rel_parts = rel.split("/")
    out = base_dir
    for part in rel_parts:
        out = out / part
    return out


def relpath_in_output(from_rel: str, to_rel: str) -> str:
    """Return a relative POSIX path from from_rel to to_rel within output.

    Both paths must be relative to output. For example, the relative path from
    `example.com/about/index.html` to `example.com/style.css` is `../style.css`.
    """
    return posixpath.relpath(to_rel, start=posixpath.dirname(from_rel))


def url_to_relative_path(target_url: str, source_page_url: str, asset_map: dict[str, str]) -> str | None:
    """Convert a target URL into a path relative to the source page.

    asset_map: {original_url: local_relative_path_from_base}
        internal asset → local relative path
        external URL  → None (the caller does not need to rewrite it)
    """
    target = strip_wayback_prefix(target_url)
    if not target or is_non_downloadable(target):
        return None

    parsed = urlparse(target)
    if not parsed.scheme or parsed.scheme not in ("http", "https"):
        # Relative path, anchor, vs.
        return target

    target_key = parsed._replace(fragment="").geturl()
    return asset_map.get(target_key, target_key)
