"""Dataset registry -- one row per released nfl_ngs_* dataset.

Tags are load-bearing (a consumer builds release URLs from them); never rename.
"""

from __future__ import annotations

from dataclasses import dataclass

RAW_ROOT_ENV = "NFL_NGS_RAW_ROOT"
CACHE_ENV = "NFL_NGS_CACHE"
DEFAULT_RAW_ROOT = "https://raw.githubusercontent.com/sportsdataverse/nfl-ngs-raw/main"
DEFAULT_CACHE = ".ngs_raw_cache"

_T = "nfl_ngs_"

# Measured floors (sdv-internal-refs/nfl/nextgenstats, 2026-09-09): the
# tracking-era surfaces answer nothing before 2016; schedule reaches 2009;
# gamecenter at least 2012. A build below a floor simply produces 0 rows.
TRACKING_FLOOR = 2016
SCHEDULE_FLOOR = 2009
GAMECENTER_FLOOR = 2012


@dataclass(frozen=True)
class DatasetSpec:
    """How to build one released dataset.

    Attributes:
        dataset: directory name under ``ngs/`` and the registry key.
        stem: output file stem (``{stem}_{season}.parquet`` / ``.csv``).
        tag: the ``sportsdataverse-data`` release tag.
        builder: key into ``reshapers.SEASON_BUILDERS`` (every NGS dataset is
            season-level: the builder enumerates the raw files it needs from
            the schedule and returns one frame).
        floor: first season with data, for docs/audit; builds below it are legal.
    """

    dataset: str
    stem: str
    tag: str
    builder: str
    floor: int = TRACKING_FLOOR


REGISTRY: dict[str, DatasetSpec] = {
    "schedules": DatasetSpec("schedules", "ngs_schedule", _T + "schedules", "schedules", SCHEDULE_FLOOR),
    "teams": DatasetSpec("teams", "ngs_teams", _T + "teams", "teams", SCHEDULE_FLOOR),
    # statboard/{passing,rushing,receiving}: one row per player x season_type x
    # week (week 0 + scope="season" = the season aggregate).
    "passing": DatasetSpec("passing", "ngs_passing", _T + "passing", "statboard"),
    "rushing": DatasetSpec("rushing", "ngs_rushing", _T + "rushing", "statboard"),
    "receiving": DatasetSpec("receiving", "ngs_receiving", _T + "receiving", "statboard"),
    # statboard/leaders: multi-category season-scope leaderboard, long by category.
    "statboard_leaders": DatasetSpec(
        "statboard_leaders", "ngs_statboard_leaders", _T + "statboard_leaders", "statboard_leaders"
    ),
    # leaders/*: all 7 families in one long table (leaderboard, scope, rank, play_*, leader_*).
    "leaders": DatasetSpec("leaders", "ngs_leaders", _T + "leaders", "leaders"),
    # gamecenter/overview, split by section; one row per game x side (x player).
    "gamecenter_passers": DatasetSpec(
        "gamecenter_passers", "ngs_gamecenter_passers", _T + "gamecenter_passers", "gc_passers", GAMECENTER_FLOOR
    ),
    "gamecenter_rushers": DatasetSpec(
        "gamecenter_rushers", "ngs_gamecenter_rushers", _T + "gamecenter_rushers", "gc_rushers", GAMECENTER_FLOOR
    ),
    "gamecenter_receivers": DatasetSpec(
        "gamecenter_receivers",
        "ngs_gamecenter_receivers",
        _T + "gamecenter_receivers",
        "gc_receivers",
        GAMECENTER_FLOOR,
    ),
    "gamecenter_pass_rushers": DatasetSpec(
        "gamecenter_pass_rushers",
        "ngs_gamecenter_pass_rushers",
        _T + "gamecenter_pass_rushers",
        "gc_pass_rushers",
        GAMECENTER_FLOOR,
    ),
    "gamecenter_leaders": DatasetSpec(
        "gamecenter_leaders", "ngs_gamecenter_leaders", _T + "gamecenter_leaders", "gc_leaders", GAMECENTER_FLOOR
    ),
}

# Release sidecar: which loader a consumer reaches the data through -- the
# unified sdv-py loader, selected by dataset= (sportsdataverse-py
# #481). Per-dataset names were deliberately NOT used:
# sdv-py already ships deprecated load_nfl_ngs_{passing,rushing,receiving}
# aliases that read nflverse's republished statboards, and reusing those names
# would silently change their source. The publish test pins that every tag
# has an entry.
PKG_FUNCTION: dict[str, str] = {
    spec.tag: f"sportsdataverse.nfl.load_nfl_ngs(dataset={key!r})" for key, spec in REGISTRY.items()
}
