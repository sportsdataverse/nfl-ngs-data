"""json -> tidy polars, one season builder per dataset.

Every NGS dataset is season-level: the builder enumerates the raw files it
needs FROM THE SCHEDULE (never a directory listing), reads each over ingest,
flattens, and returns one frame. Columns are snake_case; nested objects flatten
with a prefix (``player.gsisId`` -> ``player_gsis_id``); list-valued fields
(``playStats``, ``zones``) are dropped -- they are per-play detail with no
tidy row grain here. One canonical dtype per id at the boundary: NGS ids are
strings (``teamId`` is zero-padded, ``'0200'``), ``game_id``/``play_id`` ints.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import polars as pl

from ngs_data_build import ingest
from ngs_data_build._logging import get_logger

log = get_logger()

_C1 = re.compile(r"([a-z0-9])([A-Z])")
_C2 = re.compile(r"([A-Z]+)([A-Z][a-z])")

LEADER_FAMILIES: dict[str, tuple[str, bool]] = {
    # family: (list key, has season scope)
    "completion": ("completionLeaders", True),
    "ery": ("eryLeaders", True),
    "yac": ("yacLeaders", True),
    "distance_ballCarrier": ("leaders", False),
    "distance_tackle": ("leaders", False),
    "speed_ballCarrier": ("leaders", False),
    "time_sack": ("leaders", False),
}

ID_COLS_INT = ("game_id", "play_id", "game_key", "week", "season", "rank")


def snake(name: str) -> str:
    s = _C1.sub(r"\1_\2", name)
    s = _C2.sub(r"\1_\2", s)
    return s.replace("-", "_").lower()


def flat(obj: Any, prefix: str = "", *, drop_lists: bool = True) -> dict[str, Any]:
    """Flatten nested dicts into ``prefix_key`` columns; drop lists."""
    out: dict[str, Any] = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        key = f"{prefix}_{snake(k)}" if prefix else snake(k)
        if isinstance(v, dict):
            out.update(flat(v, key, drop_lists=drop_lists))
        elif isinstance(v, list):
            if not drop_lists:
                out[key] = v
        else:
            out[key] = v
    return out


def frame(rows: list[dict]) -> pl.DataFrame:
    """Rows -> frame with a union schema and stable id dtypes; empty -> empty frame."""
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows, infer_schema_length=None, strict=False)
    casts = []
    for c in df.columns:
        if c in ID_COLS_INT or c.endswith("_game_id") or c.endswith("_play_id"):
            casts.append(pl.col(c).cast(pl.Int64, strict=False))
        elif c.endswith("team_id") or c.endswith("_id") and df.schema[c] != pl.Int64 and c not in ("site_id",):
            casts.append(pl.col(c).cast(pl.Utf8, strict=False))
    return df.with_columns(casts) if casts else df


# --- builders ----------------------------------------------------------------

Builder = Callable[..., pl.DataFrame]


def build_schedules(season: int, *, root, downloader=None) -> pl.DataFrame:
    df = ingest.read_schedule(season, root=root, downloader=downloader)
    return df if df is not None else pl.DataFrame()


def build_teams(season: int, *, root, downloader=None) -> pl.DataFrame:
    body = ingest.read_json(f"teams/json/{season}.json", root=root, downloader=downloader)
    if not isinstance(body, list):
        return pl.DataFrame()
    rows = [{"season": season, **flat(t)} for t in body if isinstance(t, dict)]
    return frame(rows).sort(["season", "team_id"]) if rows else pl.DataFrame()


def _statboard_files(schedule: pl.DataFrame, season: int, stat: str) -> list[tuple[str, int, str, str]]:
    """(season_type, week, scope, rel) for every file the schedule implies."""
    out = []
    for st in ingest.season_types(schedule):
        out.append((st, 0, "season", f"statboard/{stat}/{season}/{st}_all.json"))
    for st, wk in ingest.week_keys(schedule):
        out.append((st, wk, "week", f"statboard/{stat}/{season}/{st}_{wk}.json"))
    return out


def build_statboard(season: int, *, root, downloader=None, stat: str) -> pl.DataFrame:
    schedule = ingest.read_schedule(season, root=root, downloader=downloader)
    if schedule is None:
        return pl.DataFrame()
    rows: list[dict] = []
    for st, wk, scope, rel in _statboard_files(schedule, season, stat):
        body = ingest.read_json(rel, root=root, downloader=downloader)
        if not isinstance(body, dict):
            continue
        for rec in body.get("stats") or []:
            if not isinstance(rec, dict):
                continue
            rec = {k: v for k, v in rec.items() if k not in ("season", "seasonType")}
            rows.append(
                {
                    "season": season,
                    "season_type": st,
                    "week": wk,
                    "scope": scope,
                    "threshold": body.get("threshold"),
                    **flat(rec),
                }
            )
    if not rows:
        return pl.DataFrame()
    df = frame(rows)
    return df.sort(["season", "season_type", "week", "team_id", "player_gsis_id"], nulls_last=True)


def build_statboard_leaders(season: int, *, root, downloader=None) -> pl.DataFrame:
    schedule = ingest.read_schedule(season, root=root, downloader=downloader)
    if schedule is None:
        return pl.DataFrame()
    rows: list[dict] = []
    for st in ingest.season_types(schedule):
        body = ingest.read_json(f"statboard/leaders/{season}/{st}_all.json", root=root, downloader=downloader)
        if not isinstance(body, dict):
            continue
        for cat, items in body.items():
            if not isinstance(items, list):
                continue
            for rank, item in enumerate(items, 1):
                if not isinstance(item, dict):
                    continue
                rows.append(
                    {
                        "season": season,
                        "season_type": st,
                        "category": snake(cat),
                        "rank": rank,
                        **flat(item.get("play") or {}, "play"),
                        **flat(item.get("leader") or {}, "leader"),
                    }
                )
    if not rows:
        return pl.DataFrame()
    return frame(rows).sort(["season", "season_type", "category", "rank"])


def build_leaders(season: int, *, root, downloader=None) -> pl.DataFrame:
    schedule = ingest.read_schedule(season, root=root, downloader=downloader)
    if schedule is None:
        return pl.DataFrame()
    rows: list[dict] = []
    types = ingest.season_types(schedule)
    weeks = ingest.week_keys(schedule)
    for fam, (key, has_season) in LEADER_FAMILIES.items():
        targets: list[tuple[str, int, str, str]] = []
        if has_season:
            targets += [(st, 0, "season", f"leaders/{fam}/{season}/{st}_all.json") for st in types]
        targets += [(st, wk, "week", f"leaders/{fam}/{season}/{st}_{wk}.json") for st, wk in weeks]
        for st, wk, scope, rel in targets:
            body = ingest.read_json(rel, root=root, downloader=downloader)
            if not isinstance(body, dict):
                continue
            for rank, item in enumerate(body.get(key) or [], 1):
                if not isinstance(item, dict):
                    continue
                rows.append(
                    {
                        "season": season,
                        "season_type": st,
                        "week": wk,
                        "scope": scope,
                        "leaderboard": snake(fam),
                        "rank": rank,
                        "league_average": body.get("leagueAverage"),
                        **flat(item.get("play") or {}, "play"),
                        **flat(item.get("leader") or {}, "leader"),
                    }
                )
    if not rows:
        return pl.DataFrame()
    return frame(rows).sort(["season", "leaderboard", "season_type", "week", "rank"])


# --- gamecenter ----------------------------------------------------------------


def _gc_iter(season: int, *, root, downloader):
    schedule = ingest.read_schedule(season, root=root, downloader=downloader)
    if schedule is None:
        return
    for gid in ingest.final_game_ids(schedule):
        body = ingest.read_json(f"gamecenter/{season}/{gid}.json", root=root, downloader=downloader)
        if isinstance(body, dict) and body.get("schedule"):
            yield gid, body


def _gc_meta(gid: int, body: dict) -> dict:
    s = body.get("schedule") or {}
    return {
        "season": s.get("season"),
        "season_type": s.get("seasonType"),
        "week": s.get("week"),
        "game_id": gid,
        "game_key": s.get("gameKey"),
        "game_date": s.get("gameDate"),
        "home_team_id": s.get("homeTeamId"),
        "home_team_abbr": s.get("homeTeamAbbr"),
        "visitor_team_id": s.get("visitorTeamId"),
        "visitor_team_abbr": s.get("visitorTeamAbbr"),
    }


def _gc_section(season: int, section: str, *, root, downloader, single: bool) -> pl.DataFrame:
    rows: list[dict] = []
    for gid, body in _gc_iter(season, root=root, downloader=downloader):
        sec = body.get(section) or {}
        if not isinstance(sec, dict):
            continue
        meta = _gc_meta(gid, body)
        extras = {snake(k): v.get("avg") for k, v in sec.items() if isinstance(v, dict) and "avg" in v}
        for side in ("home", "visitor"):
            val = sec.get(side)
            items = [val] if single else (val or [])
            for rank, item in enumerate(items, 1):
                if not isinstance(item, dict):
                    continue
                rows.append({**meta, "side": side, "rank": rank, **extras, **flat(item)})
    if not rows:
        return pl.DataFrame()
    return frame(rows).sort(["season", "game_id", "side", "rank"])


def build_gc_passers(season: int, *, root, downloader=None) -> pl.DataFrame:
    return _gc_section(season, "passers", root=root, downloader=downloader, single=True)


def build_gc_rushers(season: int, *, root, downloader=None) -> pl.DataFrame:
    return _gc_section(season, "rushers", root=root, downloader=downloader, single=False)


def build_gc_receivers(season: int, *, root, downloader=None) -> pl.DataFrame:
    return _gc_section(season, "receivers", root=root, downloader=downloader, single=False)


def build_gc_pass_rushers(season: int, *, root, downloader=None) -> pl.DataFrame:
    return _gc_section(season, "passRushers", root=root, downloader=downloader, single=False)


def build_gc_leaders(season: int, *, root, downloader=None) -> pl.DataFrame:
    rows: list[dict] = []
    for gid, body in _gc_iter(season, root=root, downloader=downloader):
        leaders = body.get("leaders") or {}
        if not isinstance(leaders, dict):
            continue
        meta = _gc_meta(gid, body)
        for cat, sides in leaders.items():
            if not isinstance(sides, dict):
                continue
            for side, item in sides.items():
                if isinstance(item, dict):
                    rows.append({**meta, "category": snake(cat), "side": side, **flat(item)})
    if not rows:
        return pl.DataFrame()
    return frame(rows).sort(["season", "game_id", "category", "side"])


SEASON_BUILDERS: dict[str, Builder] = {
    "schedules": build_schedules,
    "teams": build_teams,
    "statboard": build_statboard,  # needs stat=
    "statboard_leaders": build_statboard_leaders,
    "leaders": build_leaders,
    "gc_passers": build_gc_passers,
    "gc_rushers": build_gc_rushers,
    "gc_receivers": build_gc_receivers,
    "gc_pass_rushers": build_gc_pass_rushers,
    "gc_leaders": build_gc_leaders,
}


def build(dataset: str, builder: str, season: int, *, root: Path | str, downloader=None) -> pl.DataFrame:
    fn = SEASON_BUILDERS[builder]
    if builder == "statboard":
        return fn(season, root=root, downloader=downloader, stat=dataset)
    return fn(season, root=root, downloader=downloader)
