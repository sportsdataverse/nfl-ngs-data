"""Release publishing -- per-file ``gh release upload --clobber`` (create-if-missing).

Multi-asset globs silently drop large files, so upload one file at a time.
``runner``/``exists_check`` are injectable for hermetic tests. Every upload
re-stamps the tag's ``timestamp``/``package_function`` sidecars LAST, so the
stamp reflects the finished upload, and only when something uploaded.

One release listing per tag (the release object embeds each asset's sha256
``digest``) replaces the existence probe, and files that already match are
skipped: the droplet shares one 5,000/h GitHub quota.
"""

from __future__ import annotations

import hashlib
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


def _release_digests(tag: str, repo: str) -> dict[str, str] | None:
    """``{asset name: "sha256:<hex>"}`` for *tag* in ONE API call; ``None`` = no release.

    Only a 404 counts as absence -- a rate limit or auth failure raises.
    """
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--jq", r'.assets[] | "\(.name)\t\(.digest)"'],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        if "404" in (proc.stderr or ""):
            return None
        raise RuntimeError(f"gh api releases/tags/{tag} --repo {repo} failed: {proc.stderr.strip()}")
    return dict(ln.split("\t", 1) for ln in proc.stdout.splitlines() if "\t" in ln)


def _sha256(path: Path) -> str:
    with open(path, "rb") as fh:
        return "sha256:" + hashlib.file_digest(fh, "sha256").hexdigest()


def _dataset_files(spec: DatasetSpec, season: int, base: Path) -> list[Path]:
    root = build_io.dataset_dir(spec, base)
    # manifest before the parquet, parquet LAST: a matching parquet then means
    # every sibling of that season already landed
    cands = [root / fmt / f"{spec.stem}_{season}.{fmt}" for fmt in spec.formats if fmt != "parquet"]
    cands.append(build_io.manifest_path(spec, base))
    if "parquet" in spec.formats:
        cands.append(root / "parquet" / f"{spec.stem}_{season}.parquet")
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
    files = _dataset_files(spec, season, Path(base))
    if not files:
        log.warning("%s %s: no files to publish under %s", spec.dataset, season, base)
        return {"tag": spec.tag, "files": [], "uploaded": 0}
    remote: dict[str, str] | None = {}
    if dry_run:
        pass
    elif exists_check is not None:  # tests: old probe, upload everything
        remote = {} if exists_check(spec.tag, repo) else None
    else:
        remote = _release_digests(spec.tag, repo)
    if remote is None:
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
        remote = {}
    # The manifest re-stamps generated_at_utc on every build, so it never matches
    # itself; it and the csv derive from the parquet, so a season whose parquet
    # matches and whose siblings are all on the release is skipped whole.
    pq = files[-1] if files[-1].suffix == ".parquet" else None
    if not dry_run and pq is not None and remote.get(pq.name) == _sha256(pq) and all(f.name in remote for f in files):
        log.info("%s %s: unchanged on %s -- skipped", spec.dataset, season, spec.tag)
        return {"tag": spec.tag, "files": [str(f) for f in files], "uploaded": 0}
    count = 0
    for f in files:
        size = human_size(f.stat().st_size)
        if dry_run:
            log.info("[dry-run] upload %s (%s) -> %s:%s", f, size, repo, spec.tag)
            continue
        if f is not pq and remote.get(f.name) == _sha256(f):
            continue
        log.info("uploading %s (%s) -> %s:%s", f.name, size, repo, spec.tag)
        run(["release", "upload", spec.tag, str(f), "--repo", repo, "--clobber"])
        count += 1
    if count:
        uploaded = upload_release_sidecars(spec.tag, runner=run, pkg_function=PKG_FUNCTION.get(spec.tag), repo=repo)
        log.info("stamped %s with %s", spec.tag, ", ".join(uploaded))
    return {"tag": spec.tag, "files": [str(f) for f in files], "uploaded": count}
