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


# --- highlight plays (tracking / participation / events) -----------------------
#
# The raw stage banks one list per week plus two gzipped payloads per listed
# play. Everything here enumerates FROM THOSE LISTS (themselves enumerated from
# the schedule's weeks) -- never a directory listing.

_HL_META = ("season", "season_type", "week", "game_id", "play_id")


def _hl_items(season: int, *, root, downloader) -> list[dict]:
    """Every listed highlight in the season, de-duplicated on (gameId, playId)."""
    sched = ingest.read_schedule(season, root=root, downloader=downloader)
    if sched is None or sched.height == 0:
        return []
    seen: dict[tuple[int, int], dict] = {}
    for st, wk in ingest.week_keys(sched):
        body = ingest.read_json(f"highlights/list/{season}/{st}_{wk}.json", root=root, downloader=downloader)
        items = body.get("highlights") if isinstance(body, dict) else None
        for h in items or []:
            gid, pid = h.get("gameId"), h.get("playId")
            if gid is not None and pid is not None:
                seen.setdefault((int(gid), int(pid)), h)
    return list(seen.values())


def _hl_meta(season: int, h: dict) -> dict:
    return {
        "season": season,
        "season_type": h.get("seasonType"),
        "week": h.get("week"),
        "game_id": int(h["gameId"]),
        "play_id": int(h["playId"]),
    }


def _hl_payload(kind: str, season: int, h: dict, *, root, downloader) -> dict | None:
    rel = f"highlights/{kind}/{season}/{int(h['gameId'])}_{int(h['playId'])}.json.gz"
    body = ingest.read_json(rel, root=root, downloader=downloader)
    return body if isinstance(body, dict) else None


def build_highlights(season: int, *, root, downloader=None) -> pl.DataFrame:
    """One row per highlight play: the list item, flattened (lists dropped)."""
    rows = []
    for h in _hl_items(season, root=root, downloader=downloader):
        r = flat(h)
        for dup in ("play_game_id", "play_play_id", "season", "season_type", "week", "game_id", "play_id"):
            r.pop(dup, None)
        # the list nests the play under "play", whose own keys start with "play"
        # (playDescription, playType): "play_play_type" -> "play_type"
        r = {(k.replace("play_play_", "play_", 1) if k.startswith("play_play_") else k): v for k, v in r.items()}
        rows.append({**_hl_meta(season, h), **r})
    if not rows:
        return pl.DataFrame()
    return frame(rows).sort(["season", "week", "game_id", "play_id"])


def build_highlight_participation(season: int, *, root, downloader=None) -> pl.DataFrame:
    """One row per player on the field per highlight play, with NGS role flags
    (``was_running_route``, ``was_blitzing``, ``is_lined_up_as_qb``, ...)."""
    rows, missing = [], 0
    for h in _hl_items(season, root=root, downloader=downloader):
        body = _hl_payload("participation", season, h, root=root, downloader=downloader)
        if body is None:
            missing += 1
            continue
        meta = _hl_meta(season, h)
        for side in ("home", "away"):
            for player in body.get(side) or []:
                r = flat(player)
                for dup in _HL_META:
                    r.pop(dup, None)
                rows.append({**meta, "side": side, **r})
    if missing:
        log.warning("highlight_participation %s: %d listed plays have no raw payload yet", season, missing)
    if not rows:
        return pl.DataFrame()
    return frame(rows).sort(["season", "week", "game_id", "play_id", "side"])


def _frame_times(body: dict) -> list[str]:
    """Sorted unique sample times across every player AND the ball.

    ``frame_id`` is the 1-based index into this list, shared by tracking and
    events so they join. Times are fixed-width ISO strings, so string order is
    chronological.
    """
    times: set[str] = set()
    for side in ("homeTrackingData", "awayTrackingData"):
        for p in body.get(side) or []:
            times.update(fr["time"] for fr in p.get("playerTrackingData") or [] if fr.get("time"))
    times.update(fr["time"] for fr in body.get("ballTrackingData") or [] if fr.get("time"))
    return sorted(times)


_TRACK_SCHEMA = {
    "season": pl.Int64,
    "season_type": pl.Utf8,
    "week": pl.Int64,
    "game_id": pl.Int64,
    "play_id": pl.Int64,
    "frame_id": pl.Int64,
    "time": pl.Datetime("ms", "UTC"),
    "side": pl.Utf8,
    "team_abbr": pl.Utf8,
    "gsis_id": pl.Utf8,
    "esb_id": pl.Utf8,
    "jersey_number": pl.Int64,
    "position": pl.Utf8,
    "x": pl.Float64,
    "y": pl.Float64,
}


def _track_play(season: int, h: dict, body: dict) -> pl.DataFrame:
    """One play's frames, built columnar (18M rows/season rules out row dicts)."""
    sched = body.get("schedule") or {}
    abbr = {"home": sched.get("homeTeamAbbr"), "away": sched.get("visitorTeamAbbr"), "ball": None}
    cols: dict[str, list] = {
        k: [] for k in ("time", "x", "y", "side", "gsis_id", "esb_id", "jersey_number", "position")
    }
    entities = [("home", p) for p in body.get("homeTrackingData") or []]
    entities += [("away", p) for p in body.get("awayTrackingData") or []]
    for side, p in entities:
        frames = p.get("playerTrackingData") or []
        n = len(frames)
        cols["time"] += [fr.get("time") for fr in frames]
        cols["x"] += [fr.get("x") for fr in frames]
        cols["y"] += [fr.get("y") for fr in frames]
        cols["side"] += [side] * n
        cols["gsis_id"] += [p.get("gsisId")] * n
        cols["esb_id"] += [p.get("esbId")] * n
        cols["jersey_number"] += [p.get("jerseyNumber")] * n
        cols["position"] += [p.get("position")] * n
    ball = body.get("ballTrackingData") or []
    cols["time"] += [fr.get("time") for fr in ball]
    cols["x"] += [fr.get("x") for fr in ball]
    cols["y"] += [fr.get("y") for fr in ball]
    for k in ("gsis_id", "esb_id", "jersey_number", "position"):
        cols[k] += [None] * len(ball)
    cols["side"] += ["ball"] * len(ball)
    times = _frame_times(body)
    index = {t: i for i, t in enumerate(times, 1)}
    meta = _hl_meta(season, h)
    df = pl.DataFrame(
        {
            **{k: [v] * len(cols["time"]) for k, v in meta.items()},
            "frame_id": [index.get(t) for t in cols["time"]],
            "time": cols["time"],
            "side": cols["side"],
            "team_abbr": [abbr[s] for s in cols["side"]],
            "gsis_id": cols["gsis_id"],
            "esb_id": cols["esb_id"],
            "jersey_number": cols["jersey_number"],
            "position": cols["position"],
            "x": cols["x"],
            "y": cols["y"],
        },
        schema_overrides={k: v for k, v in _TRACK_SCHEMA.items() if k != "time"},
        strict=False,
    )
    return df.with_columns(
        pl.col("time").str.to_datetime("%Y-%m-%dT%H:%M:%S%.f", time_unit="ms", time_zone="UTC", strict=False)
    ).select(list(_TRACK_SCHEMA))


def build_highlight_tracking(season: int, *, root, downloader=None) -> pl.DataFrame:
    """One row per tracked entity (22 players + ball) per ~10 Hz frame per highlight play."""
    parts, missing = [], 0
    for h in _hl_items(season, root=root, downloader=downloader):
        body = _hl_payload("tracking", season, h, root=root, downloader=downloader)
        if body is None:
            missing += 1
            continue
        parts.append(_track_play(season, h, body))
    if missing:
        log.warning("highlight_tracking %s: %d listed plays have no raw payload yet", season, missing)
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="vertical").sort(["season", "week", "game_id", "play_id", "frame_id", "side"])


def build_highlight_events(season: int, *, root, downloader=None) -> pl.DataFrame:
    """One row per tracking event (ball_snap, pass_forward, tackle, ...), with
    the ``frame_id`` of the first frame at or after the event."""
    import bisect

    rows, missing = [], 0
    for h in _hl_items(season, root=root, downloader=downloader):
        body = _hl_payload("tracking", season, h, root=root, downloader=downloader)
        if body is None:
            missing += 1
            continue
        times = _frame_times(body)
        meta = _hl_meta(season, h)
        for ev in body.get("events") or []:
            t = ev.get("time")
            fid = None
            if t and times:
                fid = min(bisect.bisect_left(times, t), len(times) - 1) + 1
            rows.append({**meta, "event": ev.get("name"), "event_type": ev.get("type"), "time": t, "frame_id": fid})
    if missing:
        log.warning("highlight_events %s: %d listed plays have no raw payload yet", season, missing)
    if not rows:
        return pl.DataFrame()
    df = frame(rows).with_columns(
        pl.col("time").str.to_datetime("%Y-%m-%dT%H:%M:%S%.f", time_unit="ms", time_zone="UTC", strict=False)
    )
    return df.sort(["season", "week", "game_id", "play_id", "time"])


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
    "highlights": build_highlights,
    "highlight_participation": build_highlight_participation,
    "highlight_events": build_highlight_events,
    "highlight_tracking": build_highlight_tracking,
}


def build(dataset: str, builder: str, season: int, *, root: Path | str, downloader=None) -> pl.DataFrame:
    fn = SEASON_BUILDERS[builder]
    if builder == "statboard":
        return fn(season, root=root, downloader=downloader, stat=dataset)
    return fn(season, root=root, downloader=downloader)
