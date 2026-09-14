"""Dataset IO -- ``{base}/{dataset}/parquet/{stem}_{season}.parquet`` + ``csv/``.

parquet + csv only: no R package reads these tags (nflreadr reads nflverse's
own ngs-data release), so there is no ``.rds`` contract to honour. A manifest
``ngs_{dataset}_in_data_repo.csv`` is upserted per season (one row per season,
latest run wins) and uploaded beside the data so a consumer can see coverage
without listing assets.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from ngs_data_build._logging import get_logger, human_size
from ngs_data_build.config import STAGING_BASE, DatasetSpec

log = get_logger()


def resolve_base(spec: DatasetSpec, base: str | Path) -> Path:
    """``base`` for git-mirrored datasets; the gitignored staging dir otherwise."""
    return Path(base) if spec.committed else Path(STAGING_BASE)


def dataset_dir(spec: DatasetSpec, base: Path) -> Path:
    return resolve_base(spec, base) / spec.dataset


def manifest_path(spec: DatasetSpec, base: Path) -> Path:
    return dataset_dir(spec, base) / f"ngs_{spec.dataset}_in_data_repo.csv"


def _upsert_manifest(spec: DatasetSpec, season: int, row_count: int, base: Path) -> Path:
    f = manifest_path(spec, base)
    f.parent.mkdir(parents=True, exist_ok=True)
    row = pl.DataFrame(
        {
            "season": [int(season)],
            "row_count": [int(row_count)],
            "generated_at_utc": [datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")],
            "source": ["https://raw.githubusercontent.com/sportsdataverse/nfl-ngs-raw/main/ngs"],
        }
    )
    if f.exists():
        row = pl.concat([pl.read_csv(f), row], how="diagonal_relaxed")
        row = row.unique(subset=["season"], keep="last", maintain_order=True).sort("season")
    row.write_csv(f)
    return f


def write_dataset(df: pl.DataFrame, spec: DatasetSpec, season: int, *, base: str | Path = "ngs") -> list[Path]:
    """Write the spec's formats + manifest; return the data file paths."""
    base = Path(base)
    root = dataset_dir(spec, base)
    written: list[Path] = []
    for fmt in spec.formats:
        d = root / fmt
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{spec.stem}_{season}.{fmt}"
        if fmt == "parquet":
            df.write_parquet(f)
        elif fmt == "csv":
            df.write_csv(f)
        else:
            raise ValueError(f"unsupported format {fmt!r} for {spec.dataset}")
        written.append(f)
    manifest = _upsert_manifest(spec, season, df.height, base)
    log.info(
        "wrote %s, %d rows x %d cols; manifest %s%s",
        ", ".join(f"{f.name} ({human_size(f.stat().st_size)})" for f in written),
        df.height,
        df.width,
        manifest.name,
        "" if spec.committed else " [release-only staging]",
    )
    return written
