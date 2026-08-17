"""Back up this repo's one gitignored, mutable asset — the local cache DB
— to a local staging area.

Run it as:

    uv run python -m instrument_registry --backup

The staging area is meant to be copied onward to cloud storage by hand;
nothing here uploads anything. See BACKUP.md for the full rationale and
the restore procedure.

Same on-disk convention as `pothen_eshes`'s own `backup.py` (a sibling
repo, same machine): `/home/dev-ubuntu/data/backup/<repo>/`, versioned
`snapshots/<utc-timestamp>/` with a `MANIFEST.json` of sizes/SHA-256s/the
repo commit, and a `snapshots/latest` symlink. One deliberate difference:
pothen_eshes also has a `bulk/` tier for data that's large and expensive
to regenerate (1.4 GB of PDFs, a ~36-minute rebuild). Nothing here
qualifies — the whole cache DB is under 200 KB, and `refresh_athex()`/
`refresh_gleif()` take seconds — so everything, `instruments`/`entities`
(regenerable) and `instrument_aliases`/`lei_blacklist`/
`title_isin_exclusions` (not), is backed up together as one file, every
time, versioned.

Reuses `export_snapshot()` (service.py) for the actual copy rather than
reimplementing `backup()`+`serialize()` — that function exists precisely
for "pull a consistent copy of this DB out," just writing the bytes to a
local file here instead of returning them for an HTTP response body.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from instrument_registry.db.session import DEFAULT_DB_PATH
from instrument_registry.service import export_snapshot

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Default staging area, matching the convention already used by
#: pothen_eshes/euronext_athens_delisted_issuers on this machine. Lives on
#: the same WSL virtual disk as the repo, so it survives an accidental
#: delete or a bad merge but not VHDX corruption or the VM being reset.
#: Point --root at /mnt/d/... (the Windows host disk) for a copy that
#: outlives the VM.
DEFAULT_ROOT = Path("/home/dev-ubuntu/data/backup/instrument_registry")

DEFAULT_KEEP = 30

TABLES = [
    "instruments", "entities", "instrument_aliases",
    "lei_blacklist", "title_isin_exclusions",
]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(repo_root: Path) -> str | None:
    # `git_head` is a nice-to-have provenance field, so every way of not
    # getting one has to degrade to None rather than abort the backup. The
    # OSError catch is the load-bearing part: with no `git` binary on PATH,
    # subprocess.run *raises* FileNotFoundError instead of returning a
    # non-zero returncode, so the check below never runs. That killed
    # `--backup` outright inside pothen_eshes's deployed container (no git
    # in the image) — the one cache that had never been backed up at all,
    # and the only copy of any alias added through that app's own UI.
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _row_counts(db_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
    finally:
        connection.close()


def _prune(snapshots_dir: Path, keep: int) -> list[str]:
    # `not p.is_symlink()` is load-bearing: the `latest` symlink points at a
    # directory, so a bare is_dir() counts it as a snapshot — it would take up
    # a retention slot and silently prune one real snapshot too many.
    existing = sorted(p for p in snapshots_dir.iterdir()
                      if p.is_dir() and not p.is_symlink())
    doomed = existing[:-keep] if keep > 0 and len(existing) > keep else []
    for path in doomed:
        shutil.rmtree(path)
    return [p.name for p in doomed]


README = """# instrument_registry backup

Written by `uv run python -m instrument_registry --backup`. Nothing here
uploads anything — copy this folder to cloud storage yourself.

## Layout

- `snapshots/<utc-timestamp>/instrument_registry.db` — the whole cache
  DB, versioned. Small enough (well under 1 MB) that there's no separate
  `bulk/` tier the way pothen_eshes (a sibling repo, same machine) has
  for its multi-GB regenerable data — everything here is backed up
  together every time.
- Each snapshot carries a `MANIFEST.json`: size, SHA-256, the
  `instrument_registry` repo commit the backup was taken at, and a
  per-table row count.

## Important caveat

If `pothen_eshes` is running against a deployed cache (e.g. a Fly
volume), that copy can be **live and ahead of** whatever this backed up
— aliases/exclusions added through that app's UI don't reach this
machine until `export_snapshot()`'s HTTP export route is pulled down
separately. This backs up the *local* cache only.

## Restoring

- Copy the snapshot's `instrument_registry.db` to wherever `db_path`
  should point (default: the path in this file's `MANIFEST.json`
  `"source"` field). It's a plain SQLite file; verify with
  `PRAGMA integrity_check` and against the `sha256` in `MANIFEST.json`
  before trusting it.
- `instruments`/`entities` can also just be rebuilt with
  `refresh_athex()`/`refresh_gleif()` — but `instrument_aliases`/
  `lei_blacklist`/`title_isin_exclusions` cannot, so restoring the whole
  file (not just re-running the refreshes) is what actually matters.
"""


def backup(
    root: Path = DEFAULT_ROOT,
    db_path: Path | str | None = None,
    repo_root: Path = REPO_ROOT,
    keep: int = DEFAULT_KEEP,
) -> dict:
    """Runs one backup. Returns the manifest dict that was written."""
    source = Path(db_path) if db_path is not None else Path(DEFAULT_DB_PATH)

    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(README, encoding="utf-8")

    stamp = _utc_stamp()
    snapshot_dir = root / "snapshots" / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "created_utc": stamp,
        "repo_root": str(repo_root),
        "git_head": _git_head(repo_root),
        "source": str(source),
        "snapshot": {},
        "skipped": [],
    }

    if not source.exists():
        # Calling export_snapshot() on a missing path would create an empty
        # DB (connect() runs create_schema() unconditionally) and "back up"
        # five empty tables — checking existence first avoids that.
        manifest["skipped"].append("instrument_registry.db")
    else:
        dest = snapshot_dir / "instrument_registry.db"
        dest.write_bytes(export_snapshot(db_path=source))

        check = sqlite3.connect(dest)
        try:
            try:
                result = check.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as e:
                # A genuinely corrupt/truncated file fails to even open as a
                # database — same underlying failure as a bad integrity_check
                # result, just a different exception shape.
                raise RuntimeError(f"integrity check failed for {dest}: {e}") from e
        finally:
            check.close()
        if result != "ok":
            raise RuntimeError(f"integrity check failed for {dest}: {result}")

        manifest["snapshot"]["instrument_registry.db"] = {
            "bytes": dest.stat().st_size,
            "sha256": _sha256(dest),
            "integrity": result,
            "row_counts": _row_counts(dest),
        }

    manifest["pruned"] = _prune(root / "snapshots", keep)
    (snapshot_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    latest = root / "snapshots" / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(stamp)

    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up instrument_registry's local cache DB to a local "
                    "staging area for manual upload to cloud storage.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=f"staging area (default: {DEFAULT_ROOT}). Point this at a "
                             f"path on the Windows host disk (e.g. /mnt/d/...) for a "
                             f"copy that survives the WSL VM being reset.")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"versioned snapshots to retain (default: {DEFAULT_KEEP})")
    args = parser.parse_args(argv)

    try:
        manifest = backup(root=args.root, keep=args.keep)
    except Exception as e:
        print(f"instrument_registry.backup: FAILED — {e}", file=sys.stderr)
        return 1

    if "instrument_registry.db" in manifest["skipped"]:
        print("instrument_registry.backup: no cache DB found at the default "
              "path; nothing to back up.")
        return 0

    entry = manifest["snapshot"]["instrument_registry.db"]
    print(f"instrument_registry.backup: snapshot {manifest['created_utc']} -> {args.root}")
    print(f"  instrument_registry.db  {entry['bytes']/1e6:.3f} MB  sha256={entry['sha256'][:12]}...")
    print(f"  rows: {entry['row_counts']}")
    if manifest["pruned"]:
        print(f"  pruned {len(manifest['pruned'])} old snapshot(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
