"""Release publishing -- per-file ``gh release upload --clobber`` (create-if-missing).

Multi-asset globs silently drop large files, so upload one file at a time.
``runner``/``exists_check`` are injectable for hermetic tests. Every upload
re-stamps the tag's ``timestamp``/``package_function`` sidecars LAST, so the
stamp reflects the finished upload, and only when something uploaded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from sportsdataverse.release import upload_release_sidecars

from ngs_data_build import io as build_io
from ngs_data_build._logging import get_logger, human_size
from ngs_data_build.config import PKG_FUNCTION, DatasetSpec

DEFAULT_REPO = "sportsdataverse/sportsdataverse-data"

log = get_logger()


def _gh(args: list[str]) -> None:
    subprocess.run(["gh", *args], check=True, stderr=subprocess.PIPE, text=True)


def _gh_release_exists(tag: str, repo: str) -> bool:
    """Only a genuine 'not found' counts as absence -- a rate limit or auth
    failure must never be read as 'release missing' (that is what makes a
    caller run ``release create`` on an existing tag and crash the run)."""
    proc = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode == 0:
        return True
    stderr = (proc.stderr or "").strip()
    if "release not found" in stderr.lower():
        return False
    raise RuntimeError(f"gh release view {tag} --repo {repo} failed: {stderr}")


def _dataset_files(spec: DatasetSpec, season: int, base: Path) -> list[Path]:
    root = build_io.dataset_dir(spec, base)
    cands = [
        root / "parquet" / f"{spec.stem}_{season}.parquet",
        root / "csv" / f"{spec.stem}_{season}.csv",
        build_io.manifest_path(spec, base),
    ]
    return [f for f in cands if f.exists()]


def publish_dataset(
    spec: DatasetSpec,
    season: int,
    *,
    base: str | Path = "ngs",
    repo: str = DEFAULT_REPO,
    dry_run: bool = False,
    runner: Callable[[list[str]], None] | None = None,
    exists_check: Callable[[str, str], bool] | None = None,
) -> dict:
    run = runner or _gh
    exists = exists_check or _gh_release_exists
    files = _dataset_files(spec, season, Path(base))
    if not files:
        log.warning("%s %s: no files to publish under %s", spec.dataset, season, base)
        return {"tag": spec.tag, "files": [], "uploaded": 0}
    if not dry_run and not exists(spec.tag, repo):
        log.info("release %s missing on %s -- creating it", spec.tag, repo)
        try:
            run(
                [
                    "release",
                    "create",
                    spec.tag,
                    "--repo",
                    repo,
                    "--title",
                    spec.tag,
                    "--notes",
                    f"{spec.tag} (NFL Next Gen Stats dataset, built by nfl-ngs-data).",
                ]
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").lower() if isinstance(exc.stderr, str) else ""
            if "already exists" not in stderr:
                raise
            log.info("release %s already exists on %s -- continuing", spec.tag, repo)
    count = 0
    for f in files:
        size = human_size(f.stat().st_size)
        if dry_run:
            log.info("[dry-run] upload %s (%s) -> %s:%s", f, size, repo, spec.tag)
            continue
        log.info("uploading %s (%s) -> %s:%s", f.name, size, repo, spec.tag)
        run(["release", "upload", spec.tag, str(f), "--repo", repo, "--clobber"])
        count += 1
    if count:
        uploaded = upload_release_sidecars(spec.tag, runner=run, pkg_function=PKG_FUNCTION.get(spec.tag), repo=repo)
        log.info("stamped %s with %s", spec.tag, ", ".join(uploaded))
    return {"tag": spec.tag, "files": [str(f) for f in files], "uploaded": count}
