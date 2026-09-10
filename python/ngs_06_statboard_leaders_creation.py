"""Stage 06 -- statboard_leaders -> release tag ``nfl_ngs_statboard_leaders``.

Thin shim over the tested build package: the pipeline logic lives in
``ngs_data_build``; this file exists so the stage sequence is readable from a
directory listing. Equivalent to::

    python -m ngs_data_build --dataset statboard_leaders -s <start> -e <end> [--publish]
"""

from __future__ import annotations

import sys

from ngs_data_build.cli import main

DATASET = "statboard_leaders"

if __name__ == "__main__":
    # DATASET is appended, not prepended: argparse takes the last value for a
    # single-value option, so a stray --dataset cannot redirect this stage.
    sys.exit(main([*sys.argv[1:], "--dataset", DATASET]))
