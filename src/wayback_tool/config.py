"""Typed runtime configuration for the download pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DownloadOptions:
    """Validated values passed from the CLI to the pipeline."""

    url: str
    out: str = "./site"
    workers: int = 8
    max_pages: int = 200
    max_per_template: int = 1
    from_ts: str | None = None
    to_ts: str | None = None
    verbose: bool = False
