"""Read the nfl-ngs-raw tree -- over HTTPS (the normal case) or from a sibling checkout.

Copy of the canonical shape (``cfb_data_ingest/fetch.py::fetch_final``):

- URLs are BUILT from the raw repo's own schedule parquet -- a directory is
  never listed, and the raw repo is never cloned (CI cannot afford it, and a
  clone that works today is a timeout waiting for the season that adds enough
  games).
- Read-through disk cache keyed by the relative path, with a corrupt-cache
  guard: a cached file is ``json.loads``-ed before it is trusted and refetched
  once on failure, so a half-written entry from an interrupted run cannot
  poison every later build.
- Fail-soft per file with a ``None`` return; the builders count misses.
- ``downloader=`` is injectable so tests run fully offline.

``NFL_NGS_RAW_ROOT`` is either a local checkout root or the
``raw.githubusercontent.com`` base (default). ``NFL_NGS_CACHE`` is the cache dir.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Callable

import polars as pl

from ngs_data_build.config import CACHE_ENV, DEFAULT_CACHE, DEFAULT_RAW_ROOT, RAW_ROOT_ENV

Downloader = Callable[[str], bytes | None]


def _default_downloader(url: str) -> bytes | None:
    """GET -> bytes; ``None`` on 404/any failure. Bounded retries via sdv-py's gateway."""
    from sportsdataverse.dl_utils import download
    from sportsdataverse.errors import NoDataError

    try:
        resp = download(url=url, timeout=int(os.environ.get("SDV_PY_HTTP_TIMEOUT", "30")))
    except NoDataError:
        return None
    except Exception:  # noqa: BLE001 -- one bad file cannot abort the season
        return None
    if getattr(resp, "status_code", 200) != 200:
        return None
    content = getattr(resp, "content", None)
    return content if content else None


def raw_root(explicit: str | Path | None = None) -> Path | str:
    val = explicit or os.environ.get(RAW_ROOT_ENV) or DEFAULT_RAW_ROOT
    if isinstance(val, str) and val.startswith(("http://", "https://")):
        return val.rstrip("/")
    return Path(val)


def cache_root() -> Path:
    return Path(os.environ.get(CACHE_ENV, DEFAULT_CACHE))


def read_bytes(rel: str, *, root: Path | str, downloader: Downloader | None = None) -> bytes | None:
    """Bytes of ``ngs/{rel}`` from disk or HTTPS (+cache). ``None`` when absent."""
    if isinstance(root, Path):
        f = root / "ngs" / rel
        try:
            return f.read_bytes()
        except OSError:
            return None
    cached = cache_root() / rel
    if cached.exists():
        try:
            return cached.read_bytes()
        except OSError:
            pass
    body = (downloader or _default_downloader)(f"{root}/ngs/{rel}")
    if body is None:
        return None
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(cached.suffix + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, cached)
    except OSError:
        pass  # cache is best-effort; the payload is already in hand
    return body


def read_json(rel: str, *, root: Path | str, downloader: Downloader | None = None) -> dict | list | None:
    """Parsed JSON of ``ngs/{rel}``; a corrupt CACHE entry is evicted and refetched once."""
    body = read_bytes(rel, root=root, downloader=downloader)
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        if isinstance(root, Path):
            return None
        cached = cache_root() / rel
        try:
            cached.unlink()
        except OSError:
            pass
        body = (downloader or _default_downloader)(f"{root}/ngs/{rel}")
        if body is None:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None


def read_schedule(season: int, *, root: Path | str, downloader: Downloader | None = None) -> pl.DataFrame | None:
    """The raw repo's tidy per-season schedule parquet -- the enumeration surface.

    Fetched fresh every call (not cached): its ``phase`` column moves daily.
    """
    rel = f"schedules/parquet/ngs_schedule_{season}.parquet"
    if isinstance(root, Path):
        f = root / "ngs" / rel
        return pl.read_parquet(f) if f.exists() else None
    body = (downloader or _default_downloader)(f"{root}/ngs/{rel}")
    if body is None:
        return None
    try:
        return pl.read_parquet(io.BytesIO(body))
    except Exception:  # noqa: BLE001
        return None


FINAL_PHASES = ("FINAL", "FINAL OVERTIME")


def week_keys(schedule: pl.DataFrame) -> list[tuple[str, int]]:
    """Every ``(season_type, week)`` the schedule carries, in PRE/REG/POST order."""
    order = {"PRE": 0, "REG": 1, "POST": 2}
    pairs = schedule.select(["season_type", "week"]).unique().drop_nulls().to_dicts()
    return sorted(
        ((p["season_type"], int(p["week"])) for p in pairs if p["season_type"] in order),
        key=lambda t: (order[t[0]], t[1]),
    )


def season_types(schedule: pl.DataFrame) -> list[str]:
    present = set(schedule.get_column("season_type").drop_nulls().to_list())
    return [t for t in ("PRE", "REG", "POST") if t in present]


def final_game_ids(schedule: pl.DataFrame) -> list[int]:
    return schedule.filter(pl.col("phase").is_in(list(FINAL_PHASES))).get_column("game_id").cast(pl.Int64).to_list()
