"""Highlight datasets over a synthetic raw tree in nfl-ngs-raw's stage-06 layout."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import polars as pl
import pytest
from ngs_data_build import publish, reshapers
from ngs_data_build.build import build_season
from ngs_data_build.config import PKG_FUNCTION, REGISTRY, STAGING_BASE

SEASON = 2025
GID = 2025090700


def _frames(t0: int, n: int, x0: float) -> list[dict]:
    return [
        {"time": f"2025-09-07T21:32:{t0 + i // 10:02d}.{(i % 10) * 100:03d}", "x": x0 + i, "y": 20.0} for i in range(n)
    ]


def _tracking(play_id: int) -> dict:
    return {
        "gameId": GID,
        "schedule": {"homeTeamAbbr": "CHI", "visitorTeamAbbr": "GB"},
        "play": {"playId": play_id},
        "homeTrackingData": [
            {
                "gsisId": "00-1",
                "esbId": "A1",
                "position": "QB",
                "jerseyNumber": 9,
                "playerTrackingData": _frames(30, 5, 10),
            }
        ],
        "awayTrackingData": [
            {
                "gsisId": "00-2",
                "esbId": "B2",
                "position": "CB",
                "jerseyNumber": 21,
                "playerTrackingData": _frames(30, 5, 50),
            }
        ],
        # the ball starts later than the players, on its own sample times
        "ballTrackingData": [
            {"time": "2025-09-07T21:32:30.300", "x": 1.0, "y": 2.0},
            {"time": "2025-09-07T21:32:30.450", "x": 1.5, "y": 2.0},
        ],
        "events": [{"name": "ball_snap", "type": "GE", "time": "2025-09-07T21:32:30.253"}],
    }


def _participation() -> dict:
    player = {
        "gsisId": "00-1",
        "displayName": "A",
        "wasRunningRoute": False,
        "isLinedUpAsQb": True,
        "season": SEASON,
        "week": 1,
    }
    return {"home": [player], "away": [{**player, "gsisId": "00-2", "isLinedUpAsQb": False}]}


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    ngs = root / "ngs"
    (ngs / "schedules" / "parquet").mkdir(parents=True)
    pl.DataFrame(
        {"season": [SEASON], "season_type": ["REG"], "week": [1], "game_id": [GID], "phase": ["FINAL"], "iso_time": [1]}
    ).write_parquet(ngs / "schedules" / "parquet" / f"ngs_schedule_{SEASON}.parquet")
    items = [
        {
            "season": SEASON,
            "seasonType": "REG",
            "week": 1,
            "gameId": GID,
            "playId": pid,
            "teamId": "0810",
            "teamAbbr": "CHI",
            "play": {
                "gameId": GID,
                "playId": pid,
                "playDescription": "d",
                "playType": "play_type_pass",
                "playStats": [{"x": 1}],
            },
            "players": [{"gsisId": "00-1"}],
        }
        for pid in (10, 20)
    ]
    lp = ngs / "highlights" / "list" / str(SEASON) / "REG_1.json"
    lp.parent.mkdir(parents=True)
    lp.write_text(json.dumps({"total": 3, "highlights": items + [items[0]]}))  # a duplicate item must collapse
    for kind, body in (("tracking", _tracking(10)), ("participation", _participation())):
        f = ngs / "highlights" / kind / str(SEASON) / f"{GID}_10.json.gz"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(gzip.compress(json.dumps(body).encode()))
    # play 20 is listed but its payloads have not landed yet
    return root


def test_highlights_one_row_per_play_with_clean_names(raw: Path):
    df = reshapers.build_highlights(SEASON, root=raw)
    assert df.height == 2 and df["play_id"].to_list() == [10, 20]
    assert {"play_description", "play_type", "team_abbr"} <= set(df.columns)
    assert not [c for c in df.columns if c.startswith("play_play_")]
    assert "players" not in df.columns and df.schema["game_id"] == pl.Int64 and df.schema["team_id"] == pl.Utf8


def test_participation_rows_per_player_with_role_flags(raw: Path):
    df = reshapers.build_highlight_participation(SEASON, root=raw)
    assert df.height == 2 and set(df["side"]) == {"home", "away"}
    assert {"is_lined_up_as_qb", "was_running_route"} <= set(df.columns)
    assert df.columns.count("season") == 1  # the payload's own season/week do not duplicate the meta


def test_tracking_frames_share_one_clock_with_the_ball(raw: Path):
    df = reshapers.build_highlight_tracking(SEASON, root=raw)
    assert df.height == 5 + 5 + 2
    assert df.schema["time"] == pl.Datetime("ms", "UTC") and df["time"].null_count() == 0
    # frame_id indexes the union of player and ball sample times, 1-based and gap-free
    fids = sorted(df["frame_id"].unique().to_list())
    assert fids == list(range(1, len(fids) + 1))
    ball = df.filter(pl.col("side") == "ball").sort("time")
    assert ball["team_abbr"].null_count() == 2 and ball["frame_id"].to_list() == [
        4,
        6,
    ]  # 30.450 falls between player samples 30.400 and 30.500
    assert df.filter(pl.col("side") == "away")["team_abbr"].unique().to_list() == ["GB"]
    assert df.group_by(["play_id", "frame_id", "side", "gsis_id"]).len()["len"].max() == 1


def test_events_frame_is_first_sample_at_or_after(raw: Path):
    ev = reshapers.build_highlight_events(SEASON, root=raw)
    tr = reshapers.build_highlight_tracking(SEASON, root=raw)
    snap = ev.row(0, named=True)
    assert snap["event"] == "ball_snap"
    frame_time = tr.filter(pl.col("frame_id") == snap["frame_id"])["time"].min()
    assert frame_time >= snap["time"]
    earlier = tr.filter(pl.col("frame_id") == snap["frame_id"] - 1)["time"]
    assert earlier.len() == 0 or earlier.max() < snap["time"]


def test_tracking_is_release_only_parquet(raw: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "ngs"
    build_season("highlight_tracking", SEASON, base=base, raw_root=raw)
    assert not (base / "highlight_tracking").exists()  # never under the git mirror
    staged = Path(STAGING_BASE) / "highlight_tracking"
    assert (staged / "parquet" / f"ngs_highlight_tracking_{SEASON}.parquet").exists()
    assert not (staged / "csv").exists()
    calls: list[list[str]] = []
    publish.publish_dataset(
        REGISTRY["highlight_tracking"], SEASON, base=base, runner=calls.append, exists_check=lambda t, r: True
    )
    uploads = [Path(c[3]).name for c in calls if c[:2] == ["release", "upload"]]
    assert f"ngs_highlight_tracking_{SEASON}.parquet" in uploads
    assert "ngs_highlight_tracking_in_data_repo.csv" in uploads
    assert f"ngs_highlight_tracking_{SEASON}.csv" not in uploads  # no csv data file, ever


def test_committed_highlight_datasets_still_write_csv(raw: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "ngs"
    build_season("highlights", SEASON, base=base, raw_root=raw)
    assert (base / "highlights" / "csv" / f"ngs_highlights_{SEASON}.csv").exists()


def test_new_tags_have_loader_sidecar():
    for key in ("highlights", "highlight_participation", "highlight_events", "highlight_tracking"):
        assert PKG_FUNCTION[REGISTRY[key].tag] == f"sportsdataverse.nfl.load_nfl_ngs(dataset={key!r})"
