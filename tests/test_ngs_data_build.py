"""Offline tests over a synthetic raw tree in the exact nfl-ngs-raw layout.

Every read goes through ``ingest`` with a disk root (no HTTP); the HTTP path's
cache + corrupt-guard are exercised with an injected downloader.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from ngs_data_build import ingest, publish, reshapers
from ngs_data_build.build import build_season
from ngs_data_build.config import PKG_FUNCTION, REGISTRY

SEASON = 2024
GID = 2024090500


def _play(play_id: int) -> dict:
    return {
        "gameId": GID,
        "gameKey": 1,
        "playId": play_id,
        "playDescription": "x",
        "quarter": 1,
        "down": 1,
        "yardsToGo": 10,
        "possessionTeamId": "0810",
        "isSTPlay": False,
        "playStats": [{"drop": "me"}],
        "week": 1,
    }


def _leader(**kw) -> dict:
    return {"playerName": "A B", "gsisId": "00-1", "teamId": "0810", "teamAbbr": "CHI", "week": 1, **kw}


def _player() -> dict:
    return {"gsisId": "00-1", "displayName": "A B", "position": "QB", "currentTeamId": "0810", "jerseyNumber": 9}


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    """A minimal raw tree: one season, REG weeks 1-2 (2 FINAL, 1 not), one FINAL game overview."""
    root = tmp_path / "raw"
    ngs = root / "ngs"

    def w(rel: str, obj) -> None:
        p = ngs / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj), encoding="utf-8")

    sched = pl.DataFrame(
        {
            "season": [SEASON] * 3,
            "season_type": ["REG", "REG", "REG"],
            "week": [1, 1, 2],
            "game_id": [GID, GID + 1, GID + 2],
            "phase": ["FINAL", "FINAL OVERTIME", "PREGAME"],
            "iso_time": [1, 2, 3],
            "home_team_id": ["0810"] * 3,
        }
    )
    (ngs / "schedules" / "parquet").mkdir(parents=True)
    sched.write_parquet(ngs / "schedules" / "parquet" / f"ngs_schedule_{SEASON}.parquet")
    w(f"teams/json/{SEASON}.json", [{"season": SEASON, "teamId": "0810", "abbr": "CHI", "conference": {"abbr": "NFC"}}])
    rec = {
        "season": SEASON,
        "seasonType": "REG",
        "teamId": "0810",
        "player": _player(),
        "attempts": 30,
        "avgTimeToThrow": 2.5,
    }
    w(f"statboard/passing/{SEASON}/REG_all.json", {"threshold": 135, "stats": [rec]})
    w(f"statboard/passing/{SEASON}/REG_1.json", {"threshold": 10, "stats": [rec, {**rec, "teamId": "1800"}]})
    # week 2 deliberately absent (not yet scraped)
    w(
        f"statboard/leaders/{SEASON}/REG_all.json",
        {"season": SEASON, "seasonType": "REG", "fastestSacks": [{"play": _play(1), "leader": _leader(time=3.1)}]},
    )
    w(
        f"leaders/completion/{SEASON}/REG_all.json",
        {
            "completionLeaders": [
                {"play": _play(2), "leader": _leader(completionProbability=0.1, receiver={"gsisId": "00-2"})}
            ]
        },
    )
    w(
        f"leaders/time_sack/{SEASON}/REG_1.json",
        {"leagueAverage": 4.6, "leaders": [{"play": _play(3), "leader": _leader(time=2.2)}]},
    )
    w(
        f"gamecenter/{SEASON}/{GID}.json",
        {
            "schedule": {
                "season": SEASON,
                "seasonType": "REG",
                "week": 1,
                "gameId": GID,
                "homeTeamAbbr": "CHI",
                "visitorTeamAbbr": "GB",
            },
            "passers": {
                "home": {"esbId": "E1", "passYards": 200, "zones": [1, 2]},
                "visitor": {"esbId": "E2", "passYards": 150, "zones": []},
            },
            "rushers": {"home": [{"esbId": "R1", "rushYards": 50}], "visitor": []},
            "receivers": {
                "leagueAverageReceiverSeparation": {"avg": 2.9},
                "home": [{"esbId": "W1"}],
                "visitor": [{"esbId": "W2"}, {"esbId": "W3"}],
            },
            "passRushers": {"leagueAverageSeparationToQb": {"avg": 4.1}, "home": [], "visitor": [{"esbId": "D1"}]},
            "leaders": {
                "speedLeaders": {"home": {"esbId": "S1", "maxSpeed": 21.0}},
                "passDistanceLeaders": {"home": {"esbId": "P1"}, "visitor": {"esbId": "P2"}},
            },
        },
    )
    return root


def test_snake_handles_acronyms():
    assert reshapers.snake("avgYACAboveExpectation") == "avg_yac_above_expectation"
    assert reshapers.snake("isSTPlay") == "is_st_play"
    assert reshapers.snake("gsisItId") == "gsis_it_id"
    assert reshapers.snake("teamId") == "team_id"


def test_statboard_rows_and_scope(raw: Path):
    df = build_season("passing", SEASON, base=raw / "out", raw_root=raw)
    assert df.height == 3
    assert set(df.get_column("scope")) == {"season", "week"}
    assert df.filter(pl.col("week") == 0).height == 1
    assert "player_gsis_id" in df.columns and df.schema["team_id"] == pl.Utf8
    assert df.schema["week"] == pl.Int64
    assert (raw / "out" / "passing" / "parquet" / f"ngs_passing_{SEASON}.parquet").exists()
    assert (raw / "out" / "passing" / "ngs_passing_in_data_repo.csv").exists()


def test_leaders_union_drops_lists_flattens_receiver(raw: Path):
    df = build_season("leaders", SEASON, base=raw / "out", raw_root=raw)
    assert set(df.get_column("leaderboard")) == {"completion", "time_sack"}
    assert "play_play_stats" not in df.columns
    assert "leader_receiver_gsis_id" in df.columns
    assert df.filter(pl.col("leaderboard") == "time_sack").item(0, "league_average") == 4.6
    assert df.schema["play_game_id"] == pl.Int64


def test_statboard_leaders_long(raw: Path):
    df = build_season("statboard_leaders", SEASON, base=raw / "out", raw_root=raw)
    assert df.height == 1 and df.item(0, "category") == "fastest_sacks" and df.item(0, "rank") == 1


def test_gamecenter_sections_only_final_games(raw: Path):
    p = build_season("gamecenter_passers", SEASON, base=raw / "out", raw_root=raw)
    assert p.height == 2 and "zones" not in p.columns and set(p.get_column("side")) == {"home", "visitor"}
    r = build_season("gamecenter_receivers", SEASON, base=raw / "out", raw_root=raw)
    assert r.height == 3 and r.item(0, "league_average_receiver_separation") == 2.9
    ld = build_season("gamecenter_leaders", SEASON, base=raw / "out", raw_root=raw)
    assert ld.height == 3 and "speed_leaders" in set(ld.get_column("category"))
    empty = build_season("gamecenter_rushers", SEASON + 1, base=raw / "out", raw_root=raw)
    assert empty.height == 0


def test_schedules_and_teams(raw: Path):
    assert build_season("schedules", SEASON, base=raw / "out", raw_root=raw).height == 3
    t = build_season("teams", SEASON, base=raw / "out", raw_root=raw)
    assert t.item(0, "conference_abbr") == "NFC"


def test_http_cache_and_corrupt_guard(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NFL_NGS_CACHE", str(tmp_path / "cache"))
    calls: list[str] = []

    def dl(url: str):
        calls.append(url)
        return b'{"ok": 1}'

    root = "https://example.invalid/base"
    assert ingest.read_json("teams/json/2024.json", root=root, downloader=dl) == {"ok": 1}
    assert ingest.read_json("teams/json/2024.json", root=root, downloader=dl) == {"ok": 1}
    assert len(calls) == 1  # served from cache
    (tmp_path / "cache" / "teams" / "json" / "2024.json").write_text("{corrupt")
    assert ingest.read_json("teams/json/2024.json", root=root, downloader=dl) == {"ok": 1}
    assert len(calls) == 2  # corrupt entry evicted + refetched once
    assert calls[0] == f"{root}/ngs/teams/json/2024.json"
    assert ingest.read_json("nope.json", root=root, downloader=lambda u: None) is None


def test_publish_is_per_file_and_creates_missing_tag(raw: Path):
    build_season("teams", SEASON, base=raw / "out", raw_root=raw)
    ran: list[list[str]] = []
    out = publish.publish_dataset(
        REGISTRY["teams"], SEASON, base=raw / "out", runner=lambda a: ran.append(a), exists_check=lambda t, r: False
    )
    assert ran[0][:3] == ["release", "create", "nfl_ngs_teams"]
    uploads = [a for a in ran if a[:2] == ["release", "upload"]]
    data_uploads = [a for a in uploads if "ngs_teams" in a[3]]
    sidecars = [
        a
        for a in uploads
        if a[3].endswith(("timestamp.txt", "timestamp.json", "package_function.txt", "package_function.json"))
    ]
    # parquet + csv + manifest, one call each, plus the 4 tag sidecars stamped LAST
    assert out["uploaded"] == 3 and len(data_uploads) == 3 and len(sidecars) == 4
    assert all("--clobber" in a for a in uploads)
    assert uploads.index(sidecars[0]) > uploads.index(data_uploads[-1])


def test_every_tag_has_a_pkg_function():
    assert {s.tag for s in REGISTRY.values()} == set(PKG_FUNCTION)
