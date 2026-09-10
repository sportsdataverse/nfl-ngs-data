# CLAUDE.md — nfl-ngs-data

Python producer for the `nfl_ngs_*` release datasets. Reads the sibling
`nfl-ngs-raw` tree **over HTTPS, per file, enumerated from the raw schedule
parquet** — never a clone, never a directory listing — and publishes per-season
parquet + csv to `sportsdataverse-data` tags. `-data` owns reshaping and
publishing; the raw repo is read-only from here (the boundary is one-way).

## Commands (verified)

```sh
uv sync --dev
uv run pytest -q                       # offline: synthetic raw tree + injected downloader
uv run ruff check python tests
bash -n scripts/*.sh

PYTHONPATH=python .venv/bin/python -m ngs_data_build --dataset passing -s 2024 -e 2024 [--publish|--dry-run]
bash scripts/daily_ngs_data_processor.sh -s 2025 [-e 2025] [--no-publish|--dry-run]   # what CI runs
```

`NFL_NGS_RAW_ROOT` = local `nfl-ngs-raw` checkout OR the raw.githubusercontent
base (default). `NFL_NGS_CACHE` = HTTPS read-through cache (default
`.ngs_raw_cache/`, gitignored). `GH_TOKEN` for publishing.

## Conventions

- Dataset registry is `config.REGISTRY`; tags are load-bearing, never rename.
  `PKG_FUNCTION` must cover every tag (a test pins it).
- Every dataset is season-level; builders live in `reshapers.SEASON_BUILDERS`
  and enumerate raw files from `ingest.read_schedule` (`week_keys`,
  `season_types`, `final_game_ids`) — add a dataset by adding a builder + a
  registry row + a numbered shim, nothing else.
- snake_case columns; nested objects flatten with a prefix; lists are dropped;
  ids are strings except `game_id`/`play_id`/`game_key`/`week`/`season`/`rank`.
- parquet + csv only — no R loader reads these tags, so no `.rds` contract.
- Commit subject `NGS Data Update (Start: YYYY End: YYYY)` is load-bearing.
- Never add AI tools as commit co-authors. Never `uv run` in a driver.

## Gotchas

- `ingest.read_json` guards a corrupt cache entry (evict + refetch once); a
  raw file that is genuinely absent returns `None` and the builder counts on.
- The raw schedule parquet is fetched fresh every call (its `phase` column
  moves daily); everything else is cached.
- A build below a dataset's floor (`config.*_FLOOR`) legitimately returns 0
  rows and writes nothing — that is not an error.
- The season default in CI is `most_recent_nfl_season()` (rolls over after
  Labor Day). The dispatch path parses years from the raw commit subject with
  `Start:\s*\K[0-9]{4}`; a code-change push carries none → current season.
- Publishing is per-file `--clobber` (multi-asset globs drop large files) and
  create-if-missing; only a literal "release not found" counts as absence.

## Structure

```
python/ngs_data_build/{config,ingest,reshapers,build,io,publish,cli,_logging}.py
python/ngs_{01..12}_*_creation.py    numbered shims
scripts/daily_ngs_data_processor.sh  driver ; scripts/_venv.sh
ngs/{dataset}/{parquet,csv}/         committed mirror of the release assets
.github/workflows/{daily_ngs,tests,orphan_scripts}.yml
```
