"""Local backup staging area — see src/instrument_registry/backup.py and
BACKUP.md.

The point of these tests is that a backup you haven't verified is only a
belief that you have a backup: they check the copy is real, readable,
consistent, and that a missing source DB doesn't abort the run (or, worse,
silently create and "back up" an empty one).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from instrument_registry import backup as backup_mod
from instrument_registry.db.session import connect
from instrument_registry.service import add_alias


def _seed_db(db_path):
    connection = connect(db_path)
    connection.execute(
        "INSERT INTO instruments (isin, name, other_names, instrument_type, source, updated_at) "
        "VALUES ('GRS003003035', 'NAT. BANK OF GREECE SA', '[]', 'stock', 'athex', "
        "'2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()
    add_alias("GRS003003035", "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ", source="test", db_path=db_path)


def test_backup_copies_the_db_readably(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_db(db_path)

    manifest = backup_mod.backup(root=tmp_path / "dest", db_path=db_path, keep=30)

    copied = tmp_path / "dest" / "snapshots" / manifest["created_utc"] / "instrument_registry.db"
    conn = sqlite3.connect(copied)
    try:
        assert conn.execute("SELECT name FROM instruments").fetchone()[0] == "NAT. BANK OF GREECE SA"
        assert conn.execute("SELECT alias_text FROM instrument_aliases").fetchone()[0] == "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ"
    finally:
        conn.close()


def test_backup_passes_integrity_check(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_db(db_path)

    manifest = backup_mod.backup(root=tmp_path / "dest", db_path=db_path)

    assert manifest["snapshot"]["instrument_registry.db"]["integrity"] == "ok"


def test_manifest_records_verifiable_hash_and_row_counts(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_db(db_path)

    root = tmp_path / "dest"
    manifest = backup_mod.backup(root=root, db_path=db_path)
    snapshot_dir = root / "snapshots" / manifest["created_utc"]

    on_disk = json.loads((snapshot_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    assert on_disk["snapshot"] == manifest["snapshot"]
    entry = manifest["snapshot"]["instrument_registry.db"]
    assert backup_mod._sha256(snapshot_dir / "instrument_registry.db") == entry["sha256"]
    assert entry["row_counts"] == {
        "instruments": 1, "entities": 0, "instrument_aliases": 1,
        "lei_blacklist": 0, "title_isin_exclusions": 0,
    }


def test_backup_still_runs_where_there_is_no_git_binary(tmp_path, monkeypatch):
    # Confirmed live 2026-08-17: `--backup` inside pothen_eshes's deployed
    # container died with "[Errno 2] No such file or directory: 'git'"
    # before writing anything. subprocess.run raises rather than returning
    # a non-zero code when the binary is missing, so the returncode check
    # in _git_head() never got a look in. Provenance is optional; the
    # backup is not — least of all on a deployed cache, whose learned rows
    # exist nowhere else.
    db_path = tmp_path / "registry.db"
    _seed_db(db_path)

    def _no_git(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(backup_mod.subprocess, "run", _no_git)

    manifest = backup_mod.backup(root=tmp_path / "dest", db_path=db_path)

    assert manifest["git_head"] is None
    assert manifest["snapshot"]["instrument_registry.db"]["integrity"] == "ok"
    assert manifest["snapshot"]["instrument_registry.db"]["row_counts"]["instrument_aliases"] == 1


def test_missing_source_is_skipped_not_fatal(tmp_path):
    manifest = backup_mod.backup(root=tmp_path / "dest", db_path=tmp_path / "nonexistent.db")

    assert manifest["skipped"] == ["instrument_registry.db"]
    assert manifest["snapshot"] == {}


def test_missing_source_does_not_create_a_phantom_db(tmp_path):
    missing = tmp_path / "nonexistent.db"

    backup_mod.backup(root=tmp_path / "dest", db_path=missing)

    assert not missing.exists()


def test_old_snapshots_are_pruned_and_latest_points_at_newest(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_db(db_path)
    root = tmp_path / "dest"
    stamps = iter([f"2026-08-0{i}T00-00-00Z" for i in range(1, 6)])
    monkeypatch.setattr(backup_mod, "_utc_stamp", lambda: next(stamps))

    for _ in range(5):
        manifest = backup_mod.backup(root=root, db_path=db_path, keep=3)

    kept = sorted(p.name for p in (root / "snapshots").iterdir()
                  if p.is_dir() and not p.is_symlink())
    assert kept == ["2026-08-03T00-00-00Z", "2026-08-04T00-00-00Z", "2026-08-05T00-00-00Z"]
    assert (root / "snapshots" / "latest").resolve().name == manifest["created_utc"]


def test_source_db_is_never_modified(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_db(db_path)
    before = backup_mod._sha256(db_path)

    backup_mod.backup(root=tmp_path / "dest", db_path=db_path)

    assert backup_mod._sha256(db_path) == before


def test_snapshot_rejects_a_corrupt_copy(tmp_path, monkeypatch):
    """A copy that fails integrity_check must raise, not be reported as a
    successful backup — silently keeping a corrupt file is the failure mode
    this whole check exists to prevent."""
    db_path = tmp_path / "registry.db"
    _seed_db(db_path)
    monkeypatch.setattr(backup_mod, "export_snapshot", lambda **kw: b"not a valid sqlite file")

    with pytest.raises(RuntimeError, match="integrity check failed"):
        backup_mod.backup(root=tmp_path / "dest", db_path=db_path)


def test_main_reports_failure_without_raising(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(backup_mod, "backup",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("disk full")))

    assert backup_mod.main(["--root", str(tmp_path / "dest")]) == 1
    assert "disk full" in capsys.readouterr().err


def test_main_reports_row_counts(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "registry.db"
    _seed_db(db_path)
    # main() has no --db-path flag (backup() always targets the real default
    # cache outside test contexts) — patch the default so this never touches
    # the developer's actual machine-wide cache DB.
    monkeypatch.setattr(backup_mod, "DEFAULT_DB_PATH", db_path)

    exit_code = backup_mod.main(["--root", str(tmp_path / "dest")])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "instrument_registry.db" in out
