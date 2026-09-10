#!/usr/bin/env bash
# Compile the nfl_ngs_* datasets per season from nfl-ngs-raw (read over HTTPS)
# and publish each to its sportsdataverse-data release tag.
#
# Usage: bash scripts/daily_ngs_data_processor.sh -s 2025 [-e 2025] [--no-publish]
#
# The raw repo is never cloned: NFL_NGS_RAW_ROOT defaults to the
# raw.githubusercontent.com base and every file is fetched individually,
# enumerated from the raw repo's own schedule parquet (read-through cache under
# .ngs_raw_cache/, gitignored). Set NFL_NGS_RAW_ROOT=/path/to/nfl-ngs-raw for a
# local build.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

START_YEAR=""; END_YEAR=""; PUBLISH="--publish"
while [ $# -gt 0 ]; do
  case "$1" in
    -s) START_YEAR="$2"; shift 2;;
    -e) END_YEAR="$2"; shift 2;;
    --no-publish) PUBLISH=""; shift;;
    --dry-run) PUBLISH="--dry-run"; shift;;
    *) echo "usage: $0 -s <start> [-e <end>] [--no-publish|--dry-run]" >&2; exit 2;;
  esac
done
[ -n "$START_YEAR" ] || { echo "usage: $0 -s <start> [-e <end>]" >&2; exit 2; }
END_YEAR=${END_YEAR:-$START_YEAR}

export NFL_NGS_RAW_ROOT="${NFL_NGS_RAW_ROOT:-https://raw.githubusercontent.com/sportsdataverse/nfl-ngs-raw/main}"
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8

# Build order = intended dependency order (schedules first: nothing depends on
# it at build time -- every builder enumerates from the RAW schedule -- but it
# is the dataset a consumer reaches for first, so it lands first).
DATASETS="schedules teams passing rushing receiving statboard_leaders leaders gamecenter_passers gamecenter_rushers gamecenter_receivers gamecenter_pass_rushers gamecenter_leaders"

mkdir -p logs
ANY_FAILED=0; PUSH_RC=0

sdv_commit_push() {
  local msg="$1"; shift
  git add -- "$@" >/dev/null 2>&1 || true
  if git diff --cached --quiet; then echo "nothing to commit for: $msg"; return 0; fi
  git commit -q -m "$msg" || { echo "::warning ::commit failed: $msg"; return 1; }
  local attempt
  for attempt in 1 2 3; do
    if git push -q origin HEAD; then echo "pushed: $msg (attempt $attempt)"; return 0; fi
    echo "push rejected (attempt $attempt); syncing with origin"
    git fetch --quiet origin main || true
    if ! git rebase --merge origin/main >/dev/null 2>&1; then
      git rebase --abort >/dev/null 2>&1 || true
      echo "::error ::cannot rebase onto origin/main for: $msg"; return 1
    fi
  done
  echo "::error ::push still rejected after 3 attempts: $msg"; return 1
}

# shellcheck source=scripts/_venv.sh
. "$REPO/scripts/_venv.sh"
PY="$SDV_PY"
sdv_preflight ngs_data_build polars

for i in $(seq "${START_YEAR}" "${END_YEAR}"); do
  LOGFILE="logs/nfl_ngs_data_logfile_${i}.log"
  TMPLOG=$(mktemp "/tmp/nfl_ngs_data_${i}.XXXXXX.log")
  {
    git config --local user.email "action@github.com"
    git config --local user.name "Github Action"
    SEASON_RC=0
    echo "=== season $i  $(date -u '+%F %T')Z  raw=$NFL_NGS_RAW_ROOT ==="
    for ds in $DATASETS; do
      echo "::group::ngs_data_build $ds $i"
      t0=$(date +%s)
      # shellcheck disable=SC2086
      PYTHONPATH=python "$PY" -m ngs_data_build --dataset "$ds" -s "$i" -e "$i" $PUBLISH \
        || { rc=$?; echo "::warning ::ngs_data_build $ds $i rc=$rc"; SEASON_RC=$rc; }
      echo "stage $ds elapsed=$(( $(date +%s) - t0 ))s"
      echo "::endgroup::"
    done
    echo "season $i EXIT=$SEASON_RC"
    echo "$SEASON_RC" > "/tmp/_ngs_rc_${i}"
    # Load-bearing subject: downstream tooling parses the years out of it.
    sdv_commit_push "NGS Data Update (Start: $i End: $i)" ngs || echo 1 > "/tmp/_ngs_push_${i}"
  } 2>&1 | tee "$TMPLOG"
  SEASON_RC=$(cat "/tmp/_ngs_rc_${i}" 2>/dev/null || echo 1); rm -f "/tmp/_ngs_rc_${i}"
  [ -f "/tmp/_ngs_push_${i}" ] && { PUSH_RC=1; rm -f "/tmp/_ngs_push_${i}"; }
  cp "$TMPLOG" "$LOGFILE"; rm -f "$TMPLOG"
  sdv_commit_push "NGS Data log update (Start: $i End: $i)" "$LOGFILE" || PUSH_RC=1
  [ "$SEASON_RC" = "0" ] || ANY_FAILED=1
done

# A rejected push is a FAILED run, not a green one (release assets upload on a
# separate path and can succeed while the repo mirror is left stale).
[ "$PUSH_RC" = "0" ] || { echo "::error ::At least one commit failed to reach origin"; ANY_FAILED=1; }
[ "$ANY_FAILED" = "0" ] || exit 1
