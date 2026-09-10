"""CLI: ``python -m ngs_data_build --dataset <key> -s START -e END [--publish|--dry-run]``."""

from __future__ import annotations

import argparse

from ngs_data_build._logging import get_logger
from ngs_data_build.build import build_season
from ngs_data_build.config import REGISTRY

log = get_logger()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ngs_data_build")
    p.add_argument("--dataset", required=True, choices=sorted(REGISTRY))
    p.add_argument("-s", "--start", type=int, required=True)
    p.add_argument("-e", "--end", type=int, required=True)
    p.add_argument("--base", default="ngs")
    p.add_argument(
        "--raw-root", default=None, help="nfl-ngs-raw checkout OR raw.githubusercontent base (default: HTTPS)"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--publish", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    for season in range(a.start, a.end + 1):
        df = build_season(
            a.dataset,
            season,
            base=a.base,
            raw_root=a.raw_root,
            publish_release=a.publish,
            dry_run=a.dry_run,
        )
        log.info("%s %s: season complete -- %d rows", a.dataset, season, df.height)
    return 0
