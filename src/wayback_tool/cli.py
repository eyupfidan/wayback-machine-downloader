"""Command-line interface for wayback-tool."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from .config import DownloadOptions
from .pipeline import Pipeline


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser without reading process-global arguments."""
    parser = argparse.ArgumentParser(
        description="Download a static copy of a site from the Wayback Machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  wayback-tool --url "https://web.archive.org/web/20210101/https://example.com/"
  wayback-tool --url "https://example.com/about" --out ./my-site --workers 16
  wayback-tool --url "https://example.com" --max-pages 1000
""",
    )
    parser.add_argument("--url", required=True, help="Wayback or origin URL to download")
    parser.add_argument("--out", default="./site", help="Output directory (default: ./site)")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent downloads (default: 8)")
    parser.add_argument("--max-pages", type=int, default=200, help="Maximum pages (default: 200)")
    parser.add_argument(
        "--max-per-template",
        type=int,
        default=1,
        help="Maximum pages per repeatable route; 0 disables grouping (default: 1)",
    )
    parser.add_argument("--from", dest="from_ts", help="Start date (YYYYMMDD)")
    parser.add_argument("--to", dest="to_ts", help="End date (YYYYMMDD)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    return parser


def parse_options(argv: Sequence[str] | None = None) -> DownloadOptions:
    """Parse CLI arguments into typed pipeline configuration."""
    namespace = build_parser().parse_args(argv)
    return DownloadOptions(**vars(namespace))


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def main(argv: Sequence[str] | None = None) -> int:
    options = parse_options(argv)
    setup_logging(options.verbose)
    try:
        await Pipeline(options).run()
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        return 130
    except Exception as error:
        logging.exception("Unexpected error: %s", error)
        return 1
    return 0


def run() -> None:
    """Synchronous console-script entry point."""
    sys.exit(asyncio.run(main()))
