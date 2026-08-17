"""Merging learned rows between two copies of the cache — see
src/instrument_registry/merge.py.

The thing these tests are really defending is that a merge can't lose
anything. It runs against a *live* deployed cache, so the failure that
matters isn't "didn't copy a row across", it's "quietly replaced or
dropped something the destination already had". Hence the assertions
about what stayed put, not only about what arrived.
"""
from __future__ import annotations

import json

import pytest

from instrument_registry import merge as merge_mod
from instrument_registry.db.session import connect
from instrument_registry.service import add_alias, blacklist_lei, exclude_title_match


def _seed_instrument(db_path, isin="GRS003003035", name="NAT. BANK OF GREECE SA"):
    connection = connect(db_path)
    connection.execute(
        "INSERT OR IGNORE INTO instruments (isin, name, other_names, instrument_type, "
        "source, updated_at) VALUES (?, ?, '[]', 'stock', 'athex', "
        "'2026-01-01T00:00:00+00:00')",
        (isin, name),
    )
    connection.commit()
    connection.close()


def _aliases(db_path):
    connection = connect(db_path)
    try:
        return {
            (row["isin"], row["alias_text"]): row["created_at"]
            for row in connection.execute(
                "SELECT isin, alias_text, created_at FROM instrument_aliases")
        }
    finally:
        connection.close()


def test_merge_carries_missing_aliases_across(tmp_path):
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ", source="shared", db_path=source)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ", source="shared", db_path=dest)
    add_alias("GRS003003035", "ΔΙΕΘΝΗΣ ΡΟΛΙΜΕΝΑΣ", source="only-in-source", db_path=source)

    report = merge_mod.merge_learned(source, dest, apply=True)

    assert report["tables"]["instrument_aliases"]["added"] == 1
    assert report["tables"]["instrument_aliases"]["already_present"] == 1
    assert ("GRS003003035", "ΔΙΕΘΝΗΣ ΡΟΛΙΜΕΝΑΣ") in _aliases(dest)


def test_merge_preserves_created_at_rather_than_restamping_it(tmp_path):
    # A restore that rewrites created_at destroys the evidence that dates
    # a loss — which is what made the 2026-08-16 alias loss so hard to
    # place. add_alias() stamps now(); a merge must not.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="s", db_path=source)
    original = _aliases(source)[("GRS003003035", "ΕΘΝΙΚΗ")]

    merge_mod.merge_learned(source, dest, apply=True)

    assert _aliases(dest)[("GRS003003035", "ΕΘΝΙΚΗ")] == original


def test_merge_never_deletes_or_overwrites_what_the_destination_had(tmp_path):
    # The whole reason this isn't import_snapshot(): the destination is
    # live, and may have learned things the source never saw.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "FROM SOURCE", source="s", db_path=source)
    add_alias("GRS003003035", "ONLY IN DEST", source="d", db_path=dest)
    exclude_title_match("GRS003003035", "only in dest", reason="d", db_path=dest)

    merge_mod.merge_learned(source, dest, apply=True)

    keys = _aliases(dest)
    assert ("GRS003003035", "ONLY IN DEST") in keys
    assert ("GRS003003035", "FROM SOURCE") in keys
    connection = connect(dest)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM title_isin_exclusions").fetchone()[0] == 1
    finally:
        connection.close()


def test_merge_is_idempotent(tmp_path):
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="s", db_path=source)
    blacklist_lei("GRS003003035", "5299009N55YRQC69CN08", reason="wrong", db_path=source)
    exclude_title_match("GRS003003035", "quest συμμετοχων", reason="r", db_path=source)

    first = merge_mod.merge_learned(source, dest, apply=True)
    second = merge_mod.merge_learned(source, dest, apply=True)

    assert sum(t["added"] for t in first["tables"].values()) == 3
    assert sum(t["added"] for t in second["tables"].values()) == 0


def test_merge_leaves_upstream_tables_alone(tmp_path):
    # instruments/entities come from a refresh. Seeding them out of a
    # stale snapshot would plant rows upstream no longer agrees with.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    _seed_instrument(source)
    _seed_instrument(source, isin="GRS111111111", name="DELISTED CO")
    _seed_instrument(dest)

    merge_mod.merge_learned(source, dest, apply=True)

    connection = connect(dest)
    try:
        assert connection.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == 1
    finally:
        connection.close()


def test_merge_skips_and_reports_a_row_whose_isin_the_destination_lacks(tmp_path):
    # An alias for an instrument that isn't there is unreachable by every
    # lookup in the package, so inserting it would be a silent no-op with
    # a success message on top.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    _seed_instrument(source)
    _seed_instrument(source, isin="GRS111111111", name="NOT IN DEST")
    _seed_instrument(dest)
    add_alias("GRS111111111", "ORPHAN ALIAS", source="s", db_path=source)

    report = merge_mod.merge_learned(source, dest, apply=True)

    aliases = report["tables"]["instrument_aliases"]
    assert aliases["added"] == 0
    assert aliases["skipped_unknown_isin"] == 1
    assert aliases["skipped_rows"][0]["alias_text"] == "ORPHAN ALIAS"
    assert ("GRS111111111", "ORPHAN ALIAS") not in _aliases(dest)


def test_blacklist_merges_without_needing_the_instrument(tmp_path):
    # lei_blacklist deliberately has no FK to instruments — a blacklisted
    # pair outlives its instrument — so it must not be skipped like the
    # other two.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    _seed_instrument(source, isin="GRS111111111", name="NOT IN DEST")
    _seed_instrument(dest)
    blacklist_lei("GRS111111111", "5299009N55YRQC69CN08", reason="wrong", db_path=source)

    report = merge_mod.merge_learned(source, dest, apply=True)

    assert report["tables"]["lei_blacklist"]["added"] == 1
    assert report["tables"]["lei_blacklist"]["skipped_unknown_isin"] == 0


def test_writes_nothing_without_apply(tmp_path):
    # The default is a preview, because an additive merge cannot carry a
    # deletion: a row missing from the destination may be one it never
    # received, or one it deliberately deleted. Nothing here can tell
    # those apart, so a human reads the list before anything moves.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="s", db_path=source)

    report = merge_mod.merge_learned(source, dest)

    assert report["applied"] is False
    assert report["tables"]["instrument_aliases"]["added"] == 1
    assert _aliases(dest) == {}


def test_main_previews_by_default_and_says_so(tmp_path, capsys):
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="s", db_path=source)

    code = merge_mod.main([str(source), "--db-path", str(dest)])
    out = capsys.readouterr().out

    assert code == 0
    assert "--apply" in out
    assert "deliberately deleted" in out
    assert _aliases(dest) == {}


def test_a_row_the_destination_deleted_is_not_silently_reinstated(tmp_path):
    # The 2026-08-16 incident in miniature. Four aliases were deleted as
    # corrupted, then re-added from an old backup because a count
    # comparison read the cleanup as loss. A timestamp heuristic was
    # tried as the guard and rejected: add_alias() stamps now(), so the
    # re-added rows were NEWER than everything in the destination and it
    # fired on 0 of the 4 rows it existed for. What's left is that the
    # rows are shown and nothing is written without --apply.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΔΙΕΘΝΗΣ ΡΟΛΙΜΕΝΑΣ ΑΘΗΝΩΝ", source="stale", db_path=source)
    add_alias("GRS003003035", "ΔΙΕΘΝΗΣ ΑΕΡΟΛΙΜΕΝΑΣ ΑΘΗΝΩΝ", source="clean", db_path=dest)

    report = merge_mod.merge_learned(source, dest)

    assert report["tables"]["instrument_aliases"]["added_rows"][0]["alias_text"] == (
        "ΔΙΕΘΝΗΣ ΡΟΛΙΜΕΝΑΣ ΑΘΗΝΩΝ")
    assert ("GRS003003035", "ΔΙΕΘΝΗΣ ΡΟΛΙΜΕΝΑΣ ΑΘΗΝΩΝ") not in _aliases(dest)


def test_merge_never_modifies_the_source(tmp_path):
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="s", db_path=source)
    add_alias("GRS003003035", "ONLY IN DEST", source="d", db_path=dest)
    before = source.read_bytes()

    merge_mod.merge_learned(source, dest, apply=True)

    assert source.read_bytes() == before


def test_a_source_that_is_not_a_registry_cache_is_refused(tmp_path):
    source, dest = tmp_path / "random.db", tmp_path / "dest.db"
    _seed_instrument(dest)
    import sqlite3
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE something_else (x INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="not an instrument_registry cache"):
        merge_mod.merge_learned(source, dest, apply=True)


def test_main_reports_a_missing_source_without_traceback(tmp_path, capsys):
    code = merge_mod.main([str(tmp_path / "nope.db"), "--db-path", str(tmp_path / "d.db")])

    assert code == 2
    assert "no snapshot at" in capsys.readouterr().out


def test_main_emits_json_and_exits_zero(tmp_path, capsys):
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="s", db_path=source)

    code = merge_mod.main([str(source), "--db-path", str(dest), "--apply", "--json"])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tables"]["instrument_aliases"]["added"] == 1
