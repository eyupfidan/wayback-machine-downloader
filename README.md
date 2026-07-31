<div align="center">

# Wayback Machine Downloader

**Turn archived websites into clean, browsable static copies.**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.0.0-2ea44f)](https://github.com/eyupfidan/wayback-machine-downloader)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](Dockerfile)
[![GitHub stars](https://img.shields.io/github/stars/eyupfidan/wayback-machine-downloader?style=flat&logo=github)](https://github.com/eyupfidan/wayback-machine-downloader/stargazers)

[Features](#features) · [Quick start](#quick-start) · [Docker](#docker) · [CLI options](#cli-options) · [Limitations](#limitations)

</div>

Wayback Machine Downloader is a Python CLI that downloads archived HTML, CSS, JavaScript, images, and fonts, then rewrites their links for local use. The result is a portable static site that can be opened without an internet connection.

> [!NOTE]
> This project works best with static and server-rendered websites. Applications that depend on client-side rendering may require a headless browser.

## Features

- Accepts original URLs and Wayback Machine snapshot URLs
- Downloads pages and assets concurrently with automatic rate-limit backoff
- Supports an optional, pre-checked HTTP/HTTPS proxy with automatic direct fallback
- Rewrites HTML, CSS, `srcset`, and common JavaScript resource URLs
- Removes Wayback toolbar and playback artifacts
- Discovers same-origin pages with configurable limits
- Falls back to nearby snapshots when an archived resource is missing
- Generates `sitemap.json` and `sitemap.txt`
- Runs locally or in Docker

```mermaid
flowchart LR
    A[Website URL] --> B[Find Wayback snapshot]
    B --> C[Download pages and assets]
    C --> D[Clean and rewrite links]
    D --> E[Offline static site]
```

## Quick start

Requires Python 3.10 or later.

```bash
git clone https://github.com/eyupfidan/wayback-machine-downloader.git
cd wayback-machine-downloader
pip install .
```

Download an archived snapshot:

```bash
wayback-tool --url "https://web.archive.org/web/20210101000000/https://www.python.org/"
```

The site is saved to `./site` by default. Open `site/www.python.org/index.html` in your browser.

You can also run the package as a module:

```bash
python -m wayback_tool --url "https://web.archive.org/web/20210101000000/https://www.python.org/"
```

## Examples

Download a specific archived snapshot:

```bash
wayback-tool \
  --url "https://web.archive.org/web/20210101000000/https://www.python.org/"
```

Limit snapshots to a date range:

```bash
wayback-tool \
  --url "https://web.archive.org/web/20210101000000/https://www.python.org/" \
  --from 20200101 \
  --to 20231231
```

Download a larger site to a custom directory:

```bash
wayback-tool \
  --url "https://web.archive.org/web/20210101000000/https://www.python.org/" \
  --out ./archive \
  --workers 8 \
  --max-pages 500
```

Use an HTTP/HTTPS proxy (credentials are hidden in console output):

```bash
wayback-tool \
  --url "https://web.archive.org/web/20210101000000/https://www.python.org/" \
  --proxy "http://username:password@127.0.0.1:8080"
```

The downloader checks the proxy before the crawl starts. If the check fails,
it clearly reports the failure and continues without the proxy. When `--proxy`
is omitted, all requests are made directly.

## Docker

Build and run the image:

```bash
docker build -t wayback-tool .
mkdir -p site
docker run --rm \
  -v "$(pwd)/site:/output" \
  wayback-tool \
  --url "https://web.archive.org/web/20210101000000/https://www.python.org/" \
  --out /output
```

PowerShell:

```powershell
New-Item -ItemType Directory -Force site | Out-Null
docker run --rm `
  -v "${PWD}/site:/output" `
  wayback-tool `
  --url "https://web.archive.org/web/20210101000000/https://www.python.org/" `
  --out /output
```

Or use Docker Compose:

```bash
docker compose run --rm wayback \
  --url "https://web.archive.org/web/20210101000000/https://www.python.org/" \
  --out /output
```

## CLI options

| Option | Default | Description |
| --- | --- | --- |
| `--url` | required | Original website or Wayback snapshot URL |
| `--out` | `./site` | Output directory |
| `--workers` | `8` | Number of concurrent downloads |
| `--max-pages` | `200` | Maximum number of discovered pages |
| `--max-per-template` | `1` | Pages kept per repeatable route; `0` disables grouping |
| `--from` | — | Earliest snapshot date in `YYYYMMDD` format |
| `--to` | — | Latest snapshot date in `YYYYMMDD` format |
| `--proxy` | — | HTTP/HTTPS proxy URL; checked before use with direct fallback |
| `--verbose`, `-v` | off | Enable debug logging |

For the complete command reference:

```bash
wayback-tool --help
```

## Output

```text
site/
├── sitemap.json
├── sitemap.txt
└── www.python.org/
    ├── index.html
    ├── about/
    │   └── index.html
    └── assets/
        ├── css/
        ├── js/
        └── img/
```

## Limitations

- React, Vue, and other client-rendered applications are not fully supported.
- External links remain online URLs and are not downloaded.
- JavaScript URL rewriting is best-effort.
- Large archives may trigger Wayback Machine rate limits; reduce `--workers` if needed.
- Some resources may be unavailable or corrupt in the original archive.

Please use the service responsibly and respect the [Internet Archive Terms of Use](https://archive.org/about/terms.php).

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Acknowledgements

Inspired by [heikkitoivonen/wayback_downloader](https://github.com/heikkitoivonen/wayback_downloader) and [GeiserX/Wayback-Archive](https://github.com/GeiserX/Wayback-Archive). Snapshot discovery uses the [Wayback CDX Server API](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server).

## License

Distributed under the [MIT License](LICENSE).
