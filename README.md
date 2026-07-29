# wayback-tool — Wayback to Static Site Converter

Give the tool a Wayback Machine URL and it creates a **fully static, offline-ready copy** of the site. It downloads HTML, CSS, JavaScript, images, and fonts, then rewrites all links to local paths.

When you open `index.html` in a browser, the copy looks **identical to the original site** without an internet connection. The page HTML comes directly from a Wayback snapshot, so no JavaScript rendering is required.

## Installation

```bash
# Requires Python 3.10+
pip install .
```

For an editable development install with tests:

```bash
pip install -e ".[dev]"
pytest
```

The project has three runtime dependencies: `aiohttp` for concurrent HTTP requests and `beautifulsoup4` with `lxml` for HTML parsing.

## Docker

Build the image:

```bash
docker build -t wayback-tool .
```

Run it on Linux/macOS and save the result under the local `site/` directory:

```bash
mkdir -p site
docker run --rm \
    -v "$(pwd)/site:/output" \
    wayback-tool \
    --url "https://example.com" \
    --out /output \
    --workers 4 \
    --max-pages 200
```

PowerShell:

```powershell
New-Item -ItemType Directory -Force site | Out-Null
docker run --rm `
    -v "${PWD}/site:/output" `
    wayback-tool `
    --url "https://example.com" `
    --out /output `
    --workers 4 `
    --max-pages 200
```

Docker Compose builds the same image and mounts `./site` automatically:

```bash
docker compose build
docker compose run --rm wayback \
    --url "https://example.com" \
    --out /output \
    --workers 4 \
    --max-pages 200
```

The container runs as a non-root user. On Linux systems whose local user is not
UID/GID `1000`, add `--user "$(id -u):$(id -g)"` to `docker run`, or run Compose
with `docker compose run --rm --user "$(id -u):$(id -g)" wayback ...`.

## Usage

```bash
python -m wayback_tool --url "https://example.com/"
```

This command downloads the latest Wayback snapshot of `example.com` into the `site/` directory. BFS discovery downloads pages on the same origin up to `--max-pages 200`; repeatable blog/category/tag route templates keep one representative page by default.

### Options

| Flag | Default | Description |
|---|---|---|
| `--url` | required | Target URL. Supports both `https://example.com/` and `https://web.archive.org/web/20210101/https://example.com/` |
| `--out` | `./site` | Output directory |
| `--workers` | `8` | Number of concurrent downloads. A value between 4 and 16 is recommended to avoid Wayback rate limits |
| `--max-pages` | `200` | Maximum number of pages to download during BFS discovery |
| `--max-per-template` | `1` | Maximum pages kept for repeatable blog, post, category, and tag URL patterns. Use `0` to download all matching pages |
| `--from` | none | Start date in `YYYYMMDD` format, for example `20200101` |
| `--to` | none | End date in `YYYYMMDD` format |
| `--verbose`, `-v` | false | Enable debug logging |

### Examples

```bash
# Snapshot within a specific date range
python -m wayback_tool \
    --url "https://example.com/about" \
    --out ./my-site \
    --from 20200101 --to 20231231

# Slower and safer / faster with a greater risk of HTTP 429 responses
python -m wayback_tool --url "https://example.com" --workers 4
python -m wayback_tool --url "https://example.com" --workers 16

# Multi-page site
python -m wayback_tool --url "https://docs.python.org" --max-pages 500

# Keep up to three examples of each repeatable content template
python -m wayback_tool --url "https://example.com" --max-per-template 3

# Disable template grouping and download every discovered page
python -m wayback_tool --url "https://example.com" --max-per-template 0
```

## Output structure

```text
site/
├── sitemap.json              # Metadata for every page
├── sitemap.txt               # Paths only
└── example.com/
    ├── index.html            # Rewritten with relative links
    ├── about/
    │   ├── index.html
    │   └── team/
    │       └── index.html
    └── assets/
        ├── css/
        │   ├── main.css      # Rewritten url() and @import references
        │   └── theme.css
        ├── js/
        │   └── bundle.js     # Downloaded with best-effort URL rewriting
        └── img/
            ├── logo.png
            └── hero.webp
```

Open `site/example.com/index.html` in a browser. It works offline.

## How it works

1. **CDX query** — Retrieves every snapshot timestamp for the requested URL from the Wayback CDX API.
2. **Critical page capture** — For each BFS result, downloads the HTML together with its stylesheets, recursive CSS `@import` files, and external JavaScript. It then rewrites and saves the page once before BFS continues.
3. **Deferred heavy assets** — Images, fonts, video/audio, embeds, CSS `url()` resources, and best-effort JavaScript resources such as `fetch()` targets are registered with their final local paths immediately but downloaded after page discovery. Saved HTML/CSS/JS files are not reopened.
4. **Concurrent-safe downloads** — Uses `aiohttp` with rate limiting. HTTP 429 responses trigger exponential backoff at 2, 4, 8, 16, and 32 seconds. Snapshot 404 responses trigger three timestamp fallback windows: ±12 hours, ±48 hours, and ±168 hours.
5. **HTML rewriting** — Uses BeautifulSoup to process every `<a href>`, `<link href>`, `<script src>`, `<img src>`, `<source src>`, `<iframe src>`, and `srcset` attribute:
   - Removes the Wayback prefix (`/web/{ts}id_/`) from URLs.
   - Converts URLs found in `asset_map` to relative local paths.
   - Queues internal URLs that have not been downloaded yet and leaves placeholders for a later pass.
   - Keeps external URLs unchanged; they will not work offline.
6. **Wayback toolbar cleanup** — Removes `#wm-ipp-base`, `wombat.js`, `bundle-playback.js`, `banner-styles.css`, RufflePlayer scripts, and toolbar iframes.
7. **Sitemap generation** — Writes `sitemap.json` with page metadata and `sitemap.txt` with a list of paths after the deferred asset queue finishes.

## Limitations

| Limitation | Impact | Workaround |
|---|---|---|
| **SPAs that require JavaScript rendering (React/Vue)** | The tool downloads the snapshot's **HTML**. Content loaded dynamically through React, Vue, or AJAX remains empty | A headless browser is required; this tool is intended for static sites |
| **Wayback rate limits (HTTP 429)** | Downloads pause when requests are sent too quickly | Reduce `--workers` to 4–8; the tool applies backoff automatically |
| **CDX query authorization** | Wildcard searches such as `*.domain.com` may return HTTP 403 for large sites | The tool uses `matchType=exact` and discovers unknown pages through BFS |
| **Very large sites (more than 5,000 pages)** | The output can become large and downloads may take hours | Adjust the `--max-pages` limit |
| **JavaScript bundles** | URL detection in bundled JavaScript is best-effort, not exhaustive | This is sufficient for many static sites; SPAs are not supported |
| **Iframes** | The original source of deeply nested iframes may be missed | The tool downloads iframe sources, but recursion is not unlimited |
| **Corrupt fonts** | Wayback occasionally returns an HTML 404 page for `.woff` URLs | The tool detects this and removes the affected `@font-face` rule |
| **Unicode file names** | NTFS imposes file-name restrictions | `safe_filename` removes unsupported characters and generates a short name with a hash |
| **External URLs** | Links to other domains do not work offline | This is intentional: external URLs remain unchanged |

## Source layout

```text
wayback-tool/
├── pyproject.toml           # Package metadata, dependencies, and CLI entry
├── src/
│   └── wayback_tool/
│       ├── __main__.py      # python -m wayback_tool
│       ├── cli.py           # Argument parsing and console entry
│       ├── config.py        # Typed runtime configuration
│       ├── pipeline.py      # Download orchestration
│       ├── cdx.py           # CDX API client
│       ├── fetcher.py       # Downloads, retries, and fallback
│       ├── cleaner.py       # Wayback artifact cleanup
│       ├── css_processor.py # CSS dependency extraction and rewriting
│       ├── path_mapper.py   # URL-to-local-path mapping
│       ├── rewriter.py      # HTML and inline resource rewriting
│       └── sitemap.py       # BFS discovery and sitemap generation
├── tests/                   # Network-free unit tests
├── wayback_downloader.py    # Backward-compatible source wrapper
├── Dockerfile
├── compose.yaml
├── LICENSE
└── README.md
```

## References

Inspired by:

- [heikkitoivonen/wayback_downloader](https://github.com/heikkitoivonen/wayback_downloader) — Python, Requests, BeautifulSoup, and recursive crawling
- [GeiserX/Wayback-Archive](https://github.com/GeiserX/Wayback-Archive) — aggressive artifact cleanup, corrupt-font detection, and three-tier timestamp fallback
- [Internet Archive — Wayback CDX Server API](https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md) — CDX filters and `matchType`

## License

MIT.
#
