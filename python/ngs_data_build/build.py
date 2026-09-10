"""Per-season build driver: read raw (HTTPS or disk) -> reshape -> write -> (opt) publish."""

from __future__ import annotations

import time
from pathlib import Path

import polars as pl

from ngs_data_build import ingest, io, publish, reshapers
from ngs_data_build._logging import get_logger
from ngs_data_build.config import REGISTRY

log = get_logger()


def build_season(
    dataset: str,
    season: int,
    *,
    base: str | Path = "ngs",
    raw_root: str | Path | None = None,
    publish_release: bool = False,
    dry_run: bool = False,
    downloader=None,
) -> pl.DataFrame:
    """Build one dataset/season. Returns the frame (empty when nothing qualified)."""
    spec = REGISTRY[dataset]
    root = ingest.raw_root(raw_root)
    mode = "http" if isinstance(root, str) else "disk"
    started = time.monotonic()
    log.info("%s %s: build starting (raw=%s via %s)", dataset, season, root, mode)
    out = reshapers.build(dataset, spec.builder, season, root=root, downloader=downloader)
    if out.height == 0:
        log.warning(
            "%s %s: 0 rows; nothing written (below the %d floor, or raw not yet scraped)", dataset, season, spec.floor
        )
        return out
    io.write_dataset(out, spec, season, base=base)
    if publish_release or dry_run:
        publish.publish_dataset(spec, season, base=base, dry_run=dry_run)
    log.info(
        "%s %s: done -- %d rows x %d cols in %.1fs", dataset, season, out.height, out.width, time.monotonic() - started
    )
    return out
