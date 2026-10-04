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
from instrument_registry.service import (
    add_alias,
    blacklist_lei,
    exclude_title_match,
    remove_alias,
    unblacklist_lei,
)


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


# --- tombstones (#23) -------------------------------------------------------


def _tombstone_keys(db_path):
    connection = connect(db_path)
    try:
        return {
            (row["table_name"], row["isin"], row["key_text"])
            for row in connection.execute("SELECT * FROM learned_tombstones")
        }
    finally:
        connection.close()


def test_a_tombstoned_row_is_blocked_rather_than_reinstated(tmp_path):
    # The 2026-08-16 incident, with the deletion recorded: the destination
    # removed the corrupted alias after the source last wrote it, so the
    # source's copy is stale and must not come back — even under --apply.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
        add_alias("GRS003003035", "ΔΙΕΘΝΗΣ ΡΟΛΙΜΕΝΑΣ ΑΘΗΝΩΝ", source="harvest", db_path=path)
    remove_alias("GRS003003035", "ΔΙΕΘΝΗΣ ΡΟΛΙΜΕΝΑΣ ΑΘΗΝΩΝ",
                 reason="corrupted by unbounded regex", db_path=dest)

    report = merge_mod.merge_learned(source, dest, apply=True)

    aliases = report["tables"]["instrument_aliases"]
    assert aliases["added"] == 0
    assert aliases["blocked_by_tombstone"] == 1
    assert aliases["blocked_rows"][0]["deletion_reason"] == "corrupted by unbounded regex"
    assert _aliases(dest) == {}


def test_a_newer_tombstone_deletes_the_stale_destination_row(tmp_path):
    # The other direction: the cleanup was made on the source, and the
    # destination is the copy that missed it. Without this the two copies
    # never converge.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
        add_alias("GRS003003035", "ΧΑΛ Υ", source="harvest", db_path=path)
        blacklist_lei("GRS003003035", "5299009N55YRQC69CN08", reason="r", db_path=path)
    remove_alias("GRS003003035", "ΧΑΛ Υ", reason="corrupted", db_path=source)
    unblacklist_lei("GRS003003035", "5299009N55YRQC69CN08", db_path=source)

    preview = merge_mod.merge_learned(source, dest)
    assert preview["tables"]["instrument_aliases"]["deleted"] == 1
    assert ("GRS003003035", "ΧΑΛ Υ") in _aliases(dest)  # preview wrote nothing

    report = merge_mod.merge_learned(source, dest, apply=True)

    assert report["tables"]["instrument_aliases"]["deleted"] == 1
    assert report["tables"]["lei_blacklist"]["deleted"] == 1
    assert _aliases(dest) == {}
    assert ("instrument_aliases", "GRS003003035", "ΧΑΛ Υ") in _tombstone_keys(dest)
    connection = connect(dest)
    try:
        row_json = connection.execute(
            "SELECT row_json FROM learned_tombstones WHERE key_text = ?",
            ("ΧΑΛ Υ",)).fetchone()[0]
    finally:
        connection.close()
    assert json.loads(row_json)["source"] == "harvest"  # undoable


def test_a_re_add_after_the_deletion_beats_the_older_tombstone(tmp_path):
    # Newer timestamp wins: the destination deleted the alias, then the
    # source deliberately re-learned it afterwards. The re-add is the
    # later fact, so it lands and the stale tombstone goes.
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    remove_alias("GRS003003035", "ΕΘΝΙΚΗ", reason="mistake", db_path=dest)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="re-learned", db_path=source)

    report = merge_mod.merge_learned(source, dest, apply=True)

    assert report["tables"]["instrument_aliases"]["added"] == 1
    assert ("GRS003003035", "ΕΘΝΙΚΗ") in _aliases(dest)
    assert _tombstone_keys(dest) == set()


def test_an_older_tombstone_does_not_delete_a_re_added_row(tmp_path):
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    remove_alias("GRS003003035", "ΕΘΝΙΚΗ", db_path=source)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="re-learned", db_path=dest)

    report = merge_mod.merge_learned(source, dest, apply=True)

    aliases = report["tables"]["instrument_aliases"]
    assert aliases["deleted"] == 0
    assert aliases["tombstones_superseded"] == 1
    assert ("GRS003003035", "ΕΘΝΙΚΗ") in _aliases(dest)
    assert _tombstone_keys(dest) == set()


def test_tombstones_merge_both_ways_and_idempotently(tmp_path):
    # A deletion recorded on a key neither copy currently holds still
    # travels, so a stale third copy merged in later is blocked too.
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    for path in (a, b):
        _seed_instrument(path)
    remove_alias("GRS003003035", "ΡΟΛΙΜΕΝΑΣ", reason="known-bad", db_path=a)

    first = merge_mod.merge_learned(a, b, apply=True)
    second = merge_mod.merge_learned(a, b, apply=True)
    back = merge_mod.merge_learned(b, a, apply=True)

    assert first["tables"]["instrument_aliases"]["tombstones_added"] == 1
    assert second["tables"]["instrument_aliases"]["tombstones_added"] == 0
    assert back["tables"]["instrument_aliases"]["tombstones_added"] == 0
    assert _tombstone_keys(b) == {("instrument_aliases", "GRS003003035", "ΡΟΛΙΜΕΝΑΣ")}


def test_a_source_that_predates_tombstones_still_merges(tmp_path):
    # Old backups have no learned_tombstones table, and the source is
    # opened read-only so it can't be given one.
    import sqlite3
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
    add_alias("GRS003003035", "ΕΘΝΙΚΗ", source="s", db_path=source)
    raw = sqlite3.connect(source)
    raw.execute("DROP TABLE learned_tombstones")
    raw.commit()
    raw.close()

    report = merge_mod.merge_learned(source, dest, apply=True)

    assert report["tables"]["instrument_aliases"]["added"] == 1


def test_preview_lists_deletions_and_blocked_rows(tmp_path, capsys):
    source, dest = tmp_path / "source.db", tmp_path / "dest.db"
    for path in (source, dest):
        _seed_instrument(path)
        add_alias("GRS003003035", "STALE IN DEST", source="s", db_path=path)
        add_alias("GRS003003035", "STALE IN SOURCE", source="s", db_path=path)
    remove_alias("GRS003003035", "STALE IN DEST", db_path=source)
    remove_alias("GRS003003035", "STALE IN SOURCE", reason="bad", db_path=dest)

    merge_mod.main([str(source), "--db-path", str(dest)])
    out = capsys.readouterr().out

    assert "- GRS003003035  STALE IN DEST" in out
    assert "x GRS003003035  STALE IN SOURCE" in out
    assert "(bad)" in out
