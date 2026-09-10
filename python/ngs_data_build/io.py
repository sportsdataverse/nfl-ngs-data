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
from ngs_data_build.config import DatasetSpec

log = get_logger()


def dataset_dir(spec: DatasetSpec, base: Path) -> Path:
    return base / spec.dataset


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
    """Write parquet + csv + manifest; return the data file paths."""
    base = Path(base)
    root = dataset_dir(spec, base)
    pq_dir, csv_dir = root / "parquet", root / "csv"
    pq_dir.mkdir(parents=True, exist_ok=True)
    csv_dir.mkdir(parents=True, exist_ok=True)
    pq = pq_dir / f"{spec.stem}_{season}.parquet"
    csv = csv_dir / f"{spec.stem}_{season}.csv"
    df.write_parquet(pq)
    df.write_csv(csv)
    manifest = _upsert_manifest(spec, season, df.height, base)
    log.info(
        "wrote %s (%s) + %s (%s), %d rows x %d cols; manifest %s",
        pq,
        human_size(pq.stat().st_size),
        csv.name,
        human_size(csv.stat().st_size),
        df.height,
        df.width,
        manifest.name,
    )
    return [pq, csv]
