"""The read-only cache diagnostic — see src/instrument_registry/status.py.

These tests are mostly about the two real incidents that motivated the
command, reproduced as fixtures: a refresh that shipped but was never run
against a given cache (zero ETFs), and a column a refresh owns sitting
NULL on rows written before it was added (`symbol`). Both were invisible
until someone happened to look, and both are things no amount of testing
the *package* could have caught, because the defect was in the state of a
particular database rather than in the code.
"""
from __future__ import annotations

import json

from instrument_registry import backup as backup_mod
from instrument_registry import status as status_mod
from instrument_registry.db.session import connect
from instrument_registry.service import add_alias


def _seed(db_path, *, instrument_type="stock", isin="GRS003003035", symbol="ETE"):
    connection = connect(db_path)
    connection.execute(
        "INSERT INTO instruments (isin, name, other_names, instrument_type, source, "
        "updated_at, symbol) VALUES (?, 'NAT. BANK OF GREECE SA', '[]', ?, 'athex', "
        "'2026-01-01T00:00:00+00:00', ?)",
        (isin, instrument_type, symbol),
    )
    connection.commit()
    connection.close()


def test_status_reports_a_refresh_that_has_never_run_here(tmp_path):
    # The ETF case exactly: refresh_athex_etfs() shipped, the cache has
    # stocks and no ETFs at all, and nothing anywhere says so.
    db_path = tmp_path / "registry.db"
    _seed(db_path)

    report = status_mod.status(db_path=db_path, backup_root=tmp_path / "no-backups")

    etfs = next(r for r in report["refreshes"] if r["instrument_type"] == "etf")
    assert etfs["state"] == "never run here"
    assert etfs["rows"] == 0
    assert any("--refresh-athex-etfs has never been run" in p for p in report["problems"])

    stocks = next(r for r in report["refreshes"] if r["instrument_type"] == "stock")
    assert stocks["state"] == "ok"


def test_status_reports_a_column_the_refresh_owns_sitting_null(tmp_path):
    # The `symbol` case: the row exists, so "has this refresh ever run"
    # says yes — but it predates the column, and only a re-run fixes it.
    db_path = tmp_path / "registry.db"
    _seed(db_path, symbol=None)

    report = status_mod.status(db_path=db_path, backup_root=tmp_path / "no-backups")

    stocks = next(r for r in report["refreshes"] if r["instrument_type"] == "stock")
    assert stocks["state"] == "needs re-run"
    assert stocks["missing"] == {"symbol": 1}
    assert any("NULL symbol" in p and "--refresh-athex" in p for p in report["problems"])


def test_status_is_clean_when_every_refresh_has_run_completely(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed(db_path, instrument_type="stock", isin="GRS003003035", symbol="ETE")
    _seed(db_path, instrument_type="etf", isin="GRF000153004", symbol="AETF")
    backup_mod.backup(root=tmp_path / "dest", db_path=db_path, keep=30)

    report = status_mod.status(db_path=db_path, backup_root=tmp_path / "dest")

    assert report["problems"] == []
    assert all(r["state"] == "ok" for r in report["refreshes"])


def test_status_flags_a_learned_table_that_shrank_since_the_last_backup(tmp_path):
    # The alias-loss incident: four learned aliases vanished between one
    # backup and the next, and the count going *down* was the only signal
    # that existed. Nothing was watching it.
    db_path = tmp_path / "registry.db"
    _seed(db_path)
    _seed(db_path, instrument_type="etf", isin="GRF000153004", symbol="AETF")
    add_alias("GRS003003035", "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ", source="test", db_path=db_path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ", source="test", db_path=db_path)
    backup_mod.backup(root=tmp_path / "dest", db_path=db_path, keep=30)

    connection = connect(db_path)
    connection.execute("DELETE FROM instrument_aliases WHERE alias_text = 'ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ'")
    connection.commit()
    connection.close()

    report = status_mod.status(db_path=db_path, backup_root=tmp_path / "dest")

    assert report["backup"]["shrunk"] == {
        "instrument_aliases": {"backup": 2, "now": 1}
    }
    assert any("down from 2 at the last backup" in p for p in report["problems"])


def test_status_does_not_flag_upstream_tables_that_shrank(tmp_path):
    # ATHEX delisting something is normal and regenerable — only the
    # learned tables are irreplaceable, so only they alarm.
    db_path = tmp_path / "registry.db"
    _seed(db_path)
    _seed(db_path, instrument_type="stock", isin="GRS111111111", symbol="XXX")
    _seed(db_path, instrument_type="etf", isin="GRF000153004", symbol="AETF")
    backup_mod.backup(root=tmp_path / "dest", db_path=db_path, keep=30)

    connection = connect(db_path)
    connection.execute("DELETE FROM instruments WHERE isin = 'GRS111111111'")
    connection.commit()
    connection.close()

    report = status_mod.status(db_path=db_path, backup_root=tmp_path / "dest")

    assert report["backup"]["shrunk"] == {}
    assert report["problems"] == []


def test_status_reports_a_missing_backup_as_a_problem(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed(db_path)
    _seed(db_path, instrument_type="etf", isin="GRF000153004", symbol="AETF")

    report = status_mod.status(db_path=db_path, backup_root=tmp_path / "nowhere")

    assert report["backup"] is None
    assert any("no backup found" in p for p in report["problems"])


def test_status_does_not_create_the_db_it_was_pointed_at(tmp_path):
    # connect() would run create_schema() and silently manufacture an empty
    # cache, turning a wrong --db-path into a confident report about zero
    # of everything. Read-only means read-only.
    db_path = tmp_path / "definitely-not-here.db"

    report = status_mod.status(db_path=db_path, backup_root=tmp_path / "nowhere")

    assert report["exists"] is False
    assert not db_path.exists()
    assert any("no cache DB at" in p for p in report["problems"])


def test_status_flags_an_instrument_type_no_known_refresh_owns(tmp_path):
    # A guard on this module itself: adding a refresh without listing it in
    # REFRESHES would otherwise make its rows invisible to the check that
    # exists to notice invisible rows.
    db_path = tmp_path / "registry.db"
    _seed(db_path)
    _seed(db_path, instrument_type="etf", isin="GRF000153004", symbol="AETF")
    _seed(db_path, instrument_type="bond", isin="GRB000000001", symbol="BND")

    report = status_mod.status(db_path=db_path, backup_root=tmp_path / "nowhere")

    assert report["unrecognised_types"] == ["bond"]
    assert any("no known refresh owns" in p for p in report["problems"])


def test_main_exits_nonzero_when_there_are_problems(tmp_path, capsys):
    db_path = tmp_path / "registry.db"
    _seed(db_path)  # no ETFs -> a problem

    code = status_mod.main([
        "--db-path", str(db_path), "--backup-root", str(tmp_path / "dest"),
    ])

    assert code == 1
    assert "problem(s)" in capsys.readouterr().out


def test_main_exits_zero_and_can_emit_json(tmp_path, capsys):
    db_path = tmp_path / "registry.db"
    _seed(db_path)
    _seed(db_path, instrument_type="etf", isin="GRF000153004", symbol="AETF")
    backup_mod.backup(root=tmp_path / "dest", db_path=db_path, keep=30)

    code = status_mod.main([
        "--db-path", str(db_path), "--backup-root", str(tmp_path / "dest"), "--json",
    ])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []
