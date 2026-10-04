from __future__ import annotations

import json
import sqlite3

import pytest

from instrument_registry.collector.athex import AthexEtf, AthexStock
from instrument_registry.collector.gleif import GleifEntity
from instrument_registry.db.session import connect
from instrument_registry.service import (
    FeedShrinkError,
    add_alias,
    blacklist_lei,
    exclude_title_match,
    export_snapshot,
    fuzzy_match_title,
    fuzzy_match_title_scored,
    import_snapshot,
    list_aliases,
    list_blacklisted,
    list_title_exclusions,
    lookup_by_isin,
    lookup_by_lei,
    lookup_by_symbol,
    refresh_athex,
    refresh_athex_etfs,
    refresh_gleif,
    remove_alias,
    remove_title_exclusion,
    unblacklist_lei,
)


def _seed_instrument(db_path, *, isin, name, lei=None):
    connection = connect(db_path)
    connection.execute(
        """
        INSERT INTO instruments (isin, name, other_names, instrument_type, lei, source, updated_at)
        VALUES (?, ?, '[]', 'stock', ?, 'athex', '2026-01-01T00:00:00+00:00')
        """,
        (isin, name, lei),
    )
    connection.commit()
    connection.close()


def _seed_entity(db_path, *, lei, legal_name):
    connection = connect(db_path)
    connection.execute(
        """
        INSERT INTO entities (lei, legal_name, other_names, source, updated_at)
        VALUES (?, ?, '[]', 'gleif', '2026-01-01T00:00:00+00:00')
        """,
        (lei, legal_name),
    )
    connection.commit()
    connection.close()


def test_lookup_by_isin_returns_none_for_unknown_isin(tmp_path):
    db_path = tmp_path / "registry.db"
    assert lookup_by_isin("GRS003003035", db_path=db_path) is None


def test_lookup_by_isin_returns_the_seeded_instrument(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS003003035", name="NAT. BANK OF GREECE SA")

    instrument = lookup_by_isin("GRS003003035", db_path=db_path)

    assert instrument is not None
    assert instrument.name == "NAT. BANK OF GREECE SA"
    assert instrument.instrument_type == "stock"
    assert instrument.source == "athex"


def test_lookup_by_lei_returns_the_seeded_entity(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_entity(db_path, lei="5UMCZOEYKCVFAW8ZLO05", legal_name="NATIONAL BANK OF GREECE S.A.")

    entity = lookup_by_lei("5UMCZOEYKCVFAW8ZLO05", db_path=db_path)

    assert entity is not None
    assert entity.legal_name == "NATIONAL BANK OF GREECE S.A."


def test_lookup_by_symbol_returns_none_for_unknown_symbol(tmp_path):
    db_path = tmp_path / "registry.db"
    assert lookup_by_symbol("ETE", db_path=db_path) is None


def test_connect_migrates_an_existing_db_missing_the_symbol_column(tmp_path):
    # Simulates a DB created before the `symbol` column existed, to prove
    # the ALTER TABLE migration (not just CREATE TABLE IF NOT EXISTS) runs
    # for an already-deployed cache.
    db_path = tmp_path / "registry.db"
    raw = sqlite3.connect(db_path)
    raw.execute(
        """
        CREATE TABLE instruments (
            isin TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            other_names TEXT,
            cfi_code TEXT,
            instrument_type TEXT,
            currency TEXT,
            lei TEXT,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    raw.execute(
        "INSERT INTO instruments (isin, name, source, updated_at) VALUES (?, ?, ?, ?)",
        ("GRS003003035", "NAT. BANK OF GREECE SA", "athex", "2026-01-01T00:00:00+00:00"),
    )
    raw.commit()
    raw.close()

    connect(db_path).close()  # runs create_schema()/_migrate() as a side effect

    instrument = lookup_by_isin("GRS003003035", db_path=db_path)
    assert instrument is not None
    assert instrument.symbol is None  # pre-existing row, not yet backfilled
    assert lookup_by_symbol("ETE", db_path=db_path) is None  # not backfilled either


def test_connect_prints_a_one_time_notice_when_a_migration_actually_runs(tmp_path, capsys):
    # This is the safety net for the real gap that let instruments.symbol
    # sit NULL on the live cache for weeks after the migration shipped: a
    # visible nudge on whichever connect() first hits an un-migrated DB.
    db_path = tmp_path / "registry.db"
    raw = sqlite3.connect(db_path)
    raw.execute(
        """
        CREATE TABLE instruments (
            isin TEXT PRIMARY KEY, name TEXT NOT NULL, other_names TEXT,
            cfi_code TEXT, instrument_type TEXT, currency TEXT, lei TEXT,
            source TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    raw.commit()
    raw.close()

    connect(db_path).close()

    stderr = capsys.readouterr().err
    assert "instruments.symbol" in stderr
    assert "--refresh-athex" in stderr


def test_connect_does_not_repeat_the_migration_notice_on_an_already_migrated_db(tmp_path, capsys):
    db_path = tmp_path / "registry.db"
    connect(db_path).close()  # fresh DB: symbol already in SCHEMA, no ALTER needed
    capsys.readouterr()  # discard anything from the first connect

    connect(db_path).close()

    assert capsys.readouterr().err == ""


def test_fuzzy_match_title_ranks_by_similarity(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS003003035", name="NATIONAL BANK OF GREECE S.A.")
    _seed_instrument(db_path, isin="GRS831003009", name="PIRAEUS BANK S.A.")

    matches = fuzzy_match_title("NATIONAL BANK OF GREECE", threshold=0.6, db_path=db_path)

    assert [m.isin for m in matches] == ["GRS003003035"]


def test_fuzzy_match_title_scored_reflects_the_candidate_that_actually_matched(tmp_path):
    # NATIONAL BANK OF GREECE S.A. is a poor match for ATHEX's own name
    # ("NAT. BANK OF GREECE SA") but a near-exact match for the linked
    # entity's alt name — the returned ratio must reflect that, not the
    # (much lower) ratio against instrument.name alone. A caller
    # recomputing its own ratio against only instrument.name would get a
    # wrong, misleadingly low number (a real bug found live 2026-07-19).
    db_path = tmp_path / "registry.db"
    _seed_instrument(
        db_path, isin="GRS003003035", name="NAT. BANK OF GREECE SA", lei="5UMCZOEYKCVFAW8ZLO05"
    )
    _seed_entity(
        db_path,
        lei="5UMCZOEYKCVFAW8ZLO05",
        legal_name="ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΤΗΣ ΕΛΛΑΔΟΣ Α.Ε.",
    )
    connection = connect(db_path)
    connection.execute(
        "UPDATE entities SET other_names = ? WHERE lei = ?",
        ('["NATIONAL BANK OF GREECE S.A."]', "5UMCZOEYKCVFAW8ZLO05"),
    )
    connection.commit()
    connection.close()

    matches = fuzzy_match_title_scored("NATIONAL BANK OF GREECE S.A.", threshold=0.6, db_path=db_path)

    assert len(matches) == 1
    ratio, matched_via, instrument = matches[0]
    assert instrument.isin == "GRS003003035"
    assert ratio > 0.95
    assert matched_via == "NATIONAL BANK OF GREECE S.A."


def test_fuzzy_match_title_falls_back_to_the_linked_entitys_greek_name(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(
        db_path,
        isin="GRS003003035",
        name="NAT. BANK OF GREECE SA",
        lei="5UMCZOEYKCVFAW8ZLO05",
    )
    _seed_entity(
        db_path,
        lei="5UMCZOEYKCVFAW8ZLO05",
        legal_name="ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΤΗΣ ΕΛΛΑΔΟΣ Α.Ε.",
    )

    matches = fuzzy_match_title("ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΤΗΣ ΕΛΛΑΔΟΣ", threshold=0.6, db_path=db_path)

    assert [m.isin for m in matches] == ["GRS003003035"]


def test_fuzzy_match_title_respects_instrument_type_filter(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS003003035", name="NATIONAL BANK OF GREECE S.A.")

    matches = fuzzy_match_title(
        "NATIONAL BANK OF GREECE", instrument_type="bond", threshold=0.6, db_path=db_path
    )

    assert matches == []


def test_refresh_athex_upserts_without_clobbering_existing_lei(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_instrument(
        db_path, isin="GRS003003035", name="OLD NAME", lei="5UMCZOEYKCVFAW8ZLO05"
    )

    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_stocks",
        lambda: [
            AthexStock(
                isin="GRS003003035",
                symbol="ETE",
                issuer="NAT. BANK OF GREECE SA",
                issuer_full_name="NATIONAL BANK OF GREECE S.A.",
                market="SECURITIES MARKET",
            )
        ],
    )

    count = refresh_athex(db_path=db_path)

    assert count == 1
    instrument = lookup_by_isin("GRS003003035", db_path=db_path)
    assert instrument.name == "NAT. BANK OF GREECE SA"
    assert instrument.lei == "5UMCZOEYKCVFAW8ZLO05"


def test_refresh_athex_populates_the_symbol_column(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_stocks",
        lambda: [
            AthexStock(
                isin="GRS003003035",
                symbol="ETE",
                issuer="NAT. BANK OF GREECE SA",
                issuer_full_name="NATIONAL BANK OF GREECE S.A.",
                market="SECURITIES MARKET",
            )
        ],
    )

    refresh_athex(db_path=db_path)

    instrument = lookup_by_symbol("ETE", db_path=db_path)
    assert instrument is not None
    assert instrument.isin == "GRS003003035"


def test_refresh_athex_etfs_upserts_into_instruments_with_etf_type(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"

    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_etfs",
        lambda: [
            AthexEtf(
                isin="GRF000153004",
                symbol="AETF",
                issuer="ALPHA ASSET MANAGEMENT M.F.M.C.",
                issuer_full_name="ALPHA ASSET MANAGEMENT MUTUAL FUNDS MANAGEMENT COMPANY S.A.",
            )
        ],
    )

    count = refresh_athex_etfs(db_path=db_path)

    assert count == 1
    instrument = lookup_by_isin("GRF000153004", db_path=db_path)
    assert instrument is not None
    assert instrument.instrument_type == "etf"
    assert instrument.name == "ALPHA ASSET MANAGEMENT M.F.M.C."
    assert instrument.source == "athex"


def test_refresh_athex_etfs_populates_the_symbol_column(tmp_path, monkeypatch):
    # refresh_athex() has always written `symbol`; the ETF path shipped
    # without it, so an ETF ticker was unresolvable via lookup_by_symbol()
    # even though the collector had parsed it all along.
    db_path = tmp_path / "registry.db"

    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_etfs",
        lambda: [
            AthexEtf(
                isin="GRF000153004",
                symbol="AETF",
                issuer="ALPHA ASSET MANAGEMENT M.F.M.C.",
                issuer_full_name="ALPHA ASSET MANAGEMENT MUTUAL FUNDS MANAGEMENT COMPANY S.A.",
            )
        ],
    )

    refresh_athex_etfs(db_path=db_path)

    instrument = lookup_by_symbol("AETF", db_path=db_path)
    assert instrument is not None
    assert instrument.isin == "GRF000153004"
    assert instrument.instrument_type == "etf"


def test_refresh_athex_etfs_backfills_the_symbol_of_a_row_that_lacks_one(
    tmp_path, monkeypatch
):
    # The live cache already holds an ETF row written by the pre-fix code,
    # i.e. with symbol NULL. Re-running the refresh has to fill it in, or
    # the fix only helps caches that were never refreshed before it.
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRF000153004", name="ALPHA ASSET MANAGEMENT M.F.M.C.")

    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_etfs",
        lambda: [
            AthexEtf(
                isin="GRF000153004",
                symbol="AETF",
                issuer="ALPHA ASSET MANAGEMENT M.F.M.C.",
                issuer_full_name="ALPHA ASSET MANAGEMENT MUTUAL FUNDS MANAGEMENT COMPANY S.A.",
            )
        ],
    )

    refresh_athex_etfs(db_path=db_path)

    assert lookup_by_isin("GRF000153004", db_path=db_path).symbol == "AETF"


def test_refresh_athex_etfs_does_not_clobber_an_existing_lei(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_instrument(
        db_path, isin="GRF000153004", name="OLD NAME", lei="5UMCZOEYKCVFAW8ZLO05"
    )

    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_etfs",
        lambda: [
            AthexEtf(
                isin="GRF000153004",
                symbol="AETF",
                issuer="ALPHA ASSET MANAGEMENT M.F.M.C.",
                issuer_full_name="ALPHA ASSET MANAGEMENT MUTUAL FUNDS MANAGEMENT COMPANY S.A.",
            )
        ],
    )

    refresh_athex_etfs(db_path=db_path)

    instrument = lookup_by_isin("GRF000153004", db_path=db_path)
    assert instrument.name == "ALPHA ASSET MANAGEMENT M.F.M.C."
    assert instrument.lei == "5UMCZOEYKCVFAW8ZLO05"


def test_refresh_gleif_links_pending_instruments_and_stores_the_entity(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS003003035", name="NAT. BANK OF GREECE SA")

    monkeypatch.setattr(
        "instrument_registry.service.lookup_lei_by_isin",
        lambda isin: GleifEntity(
            lei="5UMCZOEYKCVFAW8ZLO05",
            legal_name="ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΤΗΣ ΕΛΛΑΔΟΣ Α.Ε.",
            other_names=["NATIONAL BANK OF GREECE S.A."],
            country="GR",
            status="ACTIVE",
        ),
    )

    result = refresh_gleif(db_path=db_path)

    assert result.linked == 1
    assert result.skipped_blacklisted == 0
    instrument = lookup_by_isin("GRS003003035", db_path=db_path)
    assert instrument.lei == "5UMCZOEYKCVFAW8ZLO05"
    entity = lookup_by_lei("5UMCZOEYKCVFAW8ZLO05", db_path=db_path)
    assert entity.legal_name == "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΤΗΣ ΕΛΛΑΔΟΣ Α.Ε."
    assert "NATIONAL BANK OF GREECE S.A." in entity.other_names


def test_blacklist_lei_clears_an_already_written_bad_link(tmp_path):
    # The real case this exists for: GLEIF's API reports Piraeus Bank's LEI
    # for Mermeren Kombinat's ISIN, and the link had already been written.
    db_path = tmp_path / "registry.db"
    _seed_entity(db_path, lei="213800OYHR1MPQ5VJL60", legal_name="PIRAEUS BANK S.A.")
    _seed_instrument(
        db_path,
        isin="GRK014011008",
        name="MERMEREN KOMBINAT A.D. PRILEP",
        lei="213800OYHR1MPQ5VJL60",
    )

    blacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", reason="wrong upstream", db_path=db_path)

    assert lookup_by_isin("GRK014011008", db_path=db_path).lei is None
    # The entity itself is a real one other instruments legitimately use.
    assert lookup_by_lei("213800OYHR1MPQ5VJL60", db_path=db_path) is not None


def test_refresh_gleif_does_not_relink_a_blacklisted_pair(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRK014011008", name="MERMEREN KOMBINAT A.D. PRILEP")
    blacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", db_path=db_path)

    monkeypatch.setattr(
        "instrument_registry.service.lookup_lei_by_isin",
        lambda isin: GleifEntity(
            lei="213800OYHR1MPQ5VJL60",
            legal_name="PIRAEUS BANK S.A.",
            other_names=[],
            country="GR",
            status="ACTIVE",
        ),
    )

    result = refresh_gleif(db_path=db_path)

    assert result.linked == 0
    # The whole point of the count: this run is distinguishable from one
    # that simply found no LEI at all.
    assert result.skipped_blacklisted == 1
    assert lookup_by_isin("GRK014011008", db_path=db_path).lei is None
    # The bogus entity row isn't created on the blacklisted pair's behalf either.
    assert lookup_by_lei("213800OYHR1MPQ5VJL60", db_path=db_path) is None


def test_refresh_gleif_still_links_a_different_lei_for_a_blacklisted_isin(tmp_path, monkeypatch):
    # Blacklisting is keyed on the pair, so an upstream fix that returns the
    # correct LEI for the same ISIN must link normally.
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRK014011008", name="MERMEREN KOMBINAT A.D. PRILEP")
    blacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", db_path=db_path)

    monkeypatch.setattr(
        "instrument_registry.service.lookup_lei_by_isin",
        lambda isin: GleifEntity(
            lei="529900W18LQJJN6SJ336",
            legal_name="MERMEREN KOMBINAT AD PRILEP",
            other_names=[],
            country="MK",
            status="ACTIVE",
        ),
    )

    result = refresh_gleif(db_path=db_path)

    assert result.linked == 1
    assert result.skipped_blacklisted == 0
    assert lookup_by_isin("GRK014011008", db_path=db_path).lei == "529900W18LQJJN6SJ336"


def test_fuzzy_match_title_matches_an_exact_learned_alias(tmp_path):
    # "MIG HOLDINGS SA" is nothing like ATHEX's own name for the instrument
    # below (ratio would be well under any reasonable threshold), but a
    # consuming project (pothen_eshes) has already confirmed via human
    # review that it's the same company — once harvested as an alias, it
    # must resolve immediately and exactly, not need refuzzy-matching.
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="ΑΝΩΝΥΜΟΣ ΕΤΑΙΡΕΙΑ ΣΥΜΜΕΤΟΧΩΝ")
    add_alias(
        "GRS332003008", "MIG HOLDINGS SA",
        source="pothen_eshes.title_review:cluster_id=366", db_path=db_path,
    )

    matches = fuzzy_match_title_scored("MIG HOLDINGS SA", threshold=0.9, db_path=db_path)

    assert len(matches) == 1
    ratio, matched_via, instrument = matches[0]
    assert instrument.isin == "GRS332003008"
    assert ratio == 1.0
    assert matched_via == "MIG HOLDINGS SA"


def test_add_alias_upserts_rather_than_duplicates(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")

    add_alias("GRS332003008", "ALIAS ONE", source="first-pass", db_path=db_path)
    add_alias("GRS332003008", "ALIAS ONE", source="second-pass", db_path=db_path)

    connection = connect(db_path)
    rows = connection.execute(
        "SELECT alias_text, source FROM instrument_aliases WHERE isin = ?", ("GRS332003008",)
    ).fetchall()
    connection.close()

    assert len(rows) == 1
    assert rows[0]["source"] == "second-pass"


def test_refresh_athex_never_touches_learned_aliases(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS003003035", name="NAT. BANK OF GREECE SA")
    add_alias("GRS003003035", "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ", source="pothen_eshes", db_path=db_path)

    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_stocks",
        lambda: [
            AthexStock(
                isin="GRS003003035", symbol="ETE", issuer="NAT. BANK OF GREECE SA",
                issuer_full_name="NATIONAL BANK OF GREECE S.A.", market="SECURITIES MARKET",
            )
        ],
    )
    refresh_athex(db_path=db_path)

    matches = fuzzy_match_title_scored("ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ", threshold=0.9, db_path=db_path)
    assert [m.isin for _, _, m in matches] == ["GRS003003035"]


def test_refresh_gleif_leaves_isin_unlinked_when_no_entity_found(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS999999999", name="SOME OLD ISSUER")

    monkeypatch.setattr(
        "instrument_registry.service.lookup_lei_by_isin", lambda isin: None
    )

    result = refresh_gleif(db_path=db_path)

    assert result.linked == 0
    # Contrast with the blacklist case: same linked count, different reason.
    assert result.skipped_blacklisted == 0
    assert lookup_by_isin("GRS999999999", db_path=db_path).lei is None


def test_exclude_title_match_removes_a_candidate_from_the_ranking(tmp_path):
    # The real QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε./ADMIE case: two genuinely unrelated
    # companies whose names share enough generic corporate-boilerplate
    # ("ΣΥΜΜΕΤΟΧΩΝ Α.Ε.") to produce a plausible-looking ratio ahead of
    # the real match.
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="ΑΔΜΗΕ ΣΥΜΜΕΤΟΧΩΝ Α.Ε.")
    _seed_instrument(db_path, isin="GRS310003009", name="QUEST ΣΥΜΜΕΤΟΧΩΝ ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ")

    before = fuzzy_match_title_scored("QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε", threshold=0.6, db_path=db_path)
    assert before[0][2].isin == "GRS518003009"  # the wrong candidate ranks first

    exclude_title_match("GRS518003009", "QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε",
                         reason="generic boilerplate overlap, not the same company", db_path=db_path)

    after = fuzzy_match_title_scored("QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε", threshold=0.6, db_path=db_path)
    assert [m.isin for _, _, m in after] == ["GRS310003009"]  # the real match now ranks first


def test_exclude_title_match_is_scoped_to_the_exact_title_normalized(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="ΑΔΜΗΕ ΣΥΜΜΕΤΟΧΩΝ Α.Ε.")

    exclude_title_match("GRS518003009", "  Quest ΣΥΜΜΕΤΟΧΩΝ Α.Ε  ", db_path=db_path)

    # Different casing/whitespace of the *same* title still excludes...
    assert fuzzy_match_title_scored("QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε", threshold=0.6, db_path=db_path) == []
    # ...but a genuinely different title is unaffected.
    matches = fuzzy_match_title_scored("ΑΔΜΗΕ ΣΥΜΜΕΤΟΧΩΝ Α.Ε.", threshold=0.6, db_path=db_path)
    assert matches[0][2].isin == "GRS518003009"


def test_exclude_title_match_upserts_rather_than_duplicates(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="SOME COMPANY")

    exclude_title_match("GRS518003009", "SOME TITLE", reason="first-pass", db_path=db_path)
    exclude_title_match("GRS518003009", "SOME TITLE", reason="second-pass", db_path=db_path)

    connection = connect(db_path)
    rows = connection.execute(
        "SELECT reason FROM title_isin_exclusions WHERE isin = ? AND title_text = ?",
        ("GRS518003009", "some title"),
    ).fetchall()
    connection.close()

    assert len(rows) == 1
    assert rows[0]["reason"] == "second-pass"


def test_remove_alias_deletes_the_row(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")
    add_alias("GRS332003008", "AN ALIAS", source="test", db_path=db_path)

    remove_alias("GRS332003008", "AN ALIAS", db_path=db_path)

    connection = connect(db_path)
    rows = connection.execute(
        "SELECT * FROM instrument_aliases WHERE isin = ? AND alias_text = ?",
        ("GRS332003008", "AN ALIAS"),
    ).fetchall()
    connection.close()
    assert rows == []


def test_remove_alias_is_a_noop_for_a_nonexistent_alias(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")

    remove_alias("GRS332003008", "NEVER ADDED", db_path=db_path)  # must not raise


def test_unblacklist_lei_deletes_the_row_without_relinking(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_entity(db_path, lei="213800OYHR1MPQ5VJL60", legal_name="PIRAEUS BANK S.A.")
    _seed_instrument(db_path, isin="GRK014011008", name="MERMEREN KOMBINAT A.D. PRILEP")
    blacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", db_path=db_path)

    unblacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", db_path=db_path)

    connection = connect(db_path)
    rows = connection.execute(
        "SELECT * FROM lei_blacklist WHERE isin = ? AND lei = ?",
        ("GRK014011008", "213800OYHR1MPQ5VJL60"),
    ).fetchall()
    connection.close()
    assert rows == []
    # Removing the blacklist entry doesn't itself relink — that's refresh_gleif()'s job.
    assert lookup_by_isin("GRK014011008", db_path=db_path).lei is None


def test_unblacklist_lei_lets_refresh_gleif_relink_the_pair(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRK014011008", name="MERMEREN KOMBINAT A.D. PRILEP")
    blacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", db_path=db_path)
    unblacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", db_path=db_path)

    monkeypatch.setattr(
        "instrument_registry.service.lookup_lei_by_isin",
        lambda isin: GleifEntity(
            lei="213800OYHR1MPQ5VJL60",
            legal_name="PIRAEUS BANK S.A.",
            other_names=[],
            country="GR",
            status="ACTIVE",
        ),
    )

    result = refresh_gleif(db_path=db_path)

    assert result.linked == 1
    assert result.skipped_blacklisted == 0
    assert lookup_by_isin("GRK014011008", db_path=db_path).lei == "213800OYHR1MPQ5VJL60"


def test_unblacklist_lei_is_a_noop_for_a_nonexistent_entry(tmp_path):
    db_path = tmp_path / "registry.db"
    unblacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", db_path=db_path)  # must not raise


def test_remove_title_exclusion_restores_the_candidate_to_ranking(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="ΑΔΜΗΕ ΣΥΜΜΕΤΟΧΩΝ Α.Ε.")
    _seed_instrument(db_path, isin="GRS310003009", name="QUEST ΣΥΜΜΕΤΟΧΩΝ ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ")
    exclude_title_match("GRS518003009", "QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε", db_path=db_path)
    assert [m.isin for _, _, m in fuzzy_match_title_scored(
        "QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε", threshold=0.6, db_path=db_path
    )] == ["GRS310003009"]

    remove_title_exclusion("GRS518003009", "QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε", db_path=db_path)

    isins = {m.isin for _, _, m in fuzzy_match_title_scored(
        "QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε", threshold=0.6, db_path=db_path
    )}
    assert "GRS518003009" in isins


def test_remove_title_exclusion_is_scoped_to_the_exact_title_normalized(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="ΑΔΜΗΕ ΣΥΜΜΕΤΟΧΩΝ Α.Ε.")
    exclude_title_match("GRS518003009", "  Quest ΣΥΜΜΕΤΟΧΩΝ Α.Ε  ", db_path=db_path)

    remove_title_exclusion("GRS518003009", "QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε", db_path=db_path)

    connection = connect(db_path)
    rows = connection.execute(
        "SELECT * FROM title_isin_exclusions WHERE isin = ?", ("GRS518003009",)
    ).fetchall()
    connection.close()
    assert rows == []


def test_remove_title_exclusion_is_a_noop_for_a_nonexistent_entry(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="SOME COMPANY")

    remove_title_exclusion("GRS518003009", "NEVER EXCLUDED", db_path=db_path)  # must not raise


def _tombstones(db_path):
    connection = connect(db_path)
    try:
        return {
            (row["table_name"], row["isin"], row["key_text"]): dict(row)
            for row in connection.execute("SELECT * FROM learned_tombstones")
        }
    finally:
        connection.close()


def test_each_remove_records_a_tombstone_carrying_the_deleted_row(tmp_path):
    # #23: a deletion has to be a row, not an absence, or a merge from a
    # stale copy cannot tell it apart from a row this copy never had.
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")
    add_alias("GRS332003008", "AN ALIAS", source="test", confidence=0.9, db_path=db_path)
    blacklist_lei("GRS332003008", "213800OYHR1MPQ5VJL60", reason="wrong", db_path=db_path)
    exclude_title_match("GRS332003008", "  Some Title ", reason="r", db_path=db_path)

    remove_alias("GRS332003008", "AN ALIAS", reason="corrupted", db_path=db_path)
    unblacklist_lei("GRS332003008", "213800OYHR1MPQ5VJL60", db_path=db_path)
    remove_title_exclusion("GRS332003008", "SOME TITLE", db_path=db_path)

    stones = _tombstones(db_path)
    assert set(stones) == {
        ("instrument_aliases", "GRS332003008", "AN ALIAS"),
        ("lei_blacklist", "GRS332003008", "213800OYHR1MPQ5VJL60"),
        ("title_isin_exclusions", "GRS332003008", "some title"),
    }
    alias = stones[("instrument_aliases", "GRS332003008", "AN ALIAS")]
    assert alias["reason"] == "corrupted"
    assert json.loads(alias["row_json"])["confidence"] == 0.9


def test_removing_a_key_that_is_not_there_still_records_the_deletion(tmp_path):
    # How a cleanup another copy hasn't had yet gets recorded — including
    # the four aliases deleted on 2026-08-09, before tombstones existed.
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")

    remove_alias("GRS332003008", "NEVER ADDED", reason="known-bad", db_path=db_path)

    stone = _tombstones(db_path)[("instrument_aliases", "GRS332003008", "NEVER ADDED")]
    assert stone["row_json"] is None


def test_re_removing_keeps_the_original_deleted_at(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")
    remove_alias("GRS332003008", "X", reason="first", db_path=db_path)
    first = _tombstones(db_path)[("instrument_aliases", "GRS332003008", "X")]

    remove_alias("GRS332003008", "X", db_path=db_path)

    again = _tombstones(db_path)[("instrument_aliases", "GRS332003008", "X")]
    assert again["deleted_at"] == first["deleted_at"]
    assert again["reason"] == "first"


def test_re_adding_a_deleted_key_clears_its_tombstone(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")
    add_alias("GRS332003008", "A", source="t", db_path=db_path)
    blacklist_lei("GRS332003008", "213800OYHR1MPQ5VJL60", db_path=db_path)
    exclude_title_match("GRS332003008", "t", db_path=db_path)
    remove_alias("GRS332003008", "A", db_path=db_path)
    unblacklist_lei("GRS332003008", "213800OYHR1MPQ5VJL60", db_path=db_path)
    remove_title_exclusion("GRS332003008", "t", db_path=db_path)

    add_alias("GRS332003008", "A", source="t", db_path=db_path)
    blacklist_lei("GRS332003008", "213800OYHR1MPQ5VJL60", db_path=db_path)
    exclude_title_match("GRS332003008", " T ", db_path=db_path)

    assert _tombstones(db_path) == {}


def test_list_aliases_returns_all_aliases_for_the_isin_oldest_first(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")
    _seed_instrument(db_path, isin="GRS999999999", name="A DIFFERENT COMPANY")
    add_alias("GRS332003008", "ALIAS ONE", source="first-pass", confidence=0.8, db_path=db_path)
    add_alias("GRS332003008", "ALIAS TWO", source="second-pass", db_path=db_path)
    add_alias("GRS999999999", "UNRELATED ALIAS", source="test", db_path=db_path)

    aliases = list_aliases("GRS332003008", db_path=db_path)

    assert [a.alias_text for a in aliases] == ["ALIAS ONE", "ALIAS TWO"]
    assert aliases[0].source == "first-pass"
    assert aliases[0].confidence == 0.8


def test_list_aliases_returns_empty_for_an_isin_with_none(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")

    assert list_aliases("GRS332003008", db_path=db_path) == []


def test_list_blacklisted_returns_all_pairs_when_isin_omitted(tmp_path):
    db_path = tmp_path / "registry.db"
    blacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", reason="wrong upstream", db_path=db_path)
    blacklist_lei("GRS999999999", "5UMCZOEYKCVFAW8ZLO05", db_path=db_path)

    entries = list_blacklisted(db_path=db_path)

    assert {(e.isin, e.lei) for e in entries} == {
        ("GRK014011008", "213800OYHR1MPQ5VJL60"),
        ("GRS999999999", "5UMCZOEYKCVFAW8ZLO05"),
    }


def test_list_blacklisted_filters_by_isin_when_given(tmp_path):
    db_path = tmp_path / "registry.db"
    blacklist_lei("GRK014011008", "213800OYHR1MPQ5VJL60", reason="wrong upstream", db_path=db_path)
    blacklist_lei("GRS999999999", "5UMCZOEYKCVFAW8ZLO05", db_path=db_path)

    entries = list_blacklisted(isin="GRK014011008", db_path=db_path)

    assert len(entries) == 1
    assert entries[0].lei == "213800OYHR1MPQ5VJL60"
    assert entries[0].reason == "wrong upstream"


def test_list_title_exclusions_returns_all_exclusions_for_the_isin(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="ΑΔΜΗΕ ΣΥΜΜΕΤΟΧΩΝ Α.Ε.")
    exclude_title_match(
        "GRS518003009", "QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε",
        reason="generic boilerplate overlap, not the same company", db_path=db_path,
    )

    exclusions = list_title_exclusions("GRS518003009", db_path=db_path)

    assert len(exclusions) == 1
    assert exclusions[0].title_text == "quest συμμετοχων α.ε"
    assert exclusions[0].reason == "generic boilerplate overlap, not the same company"


def test_list_title_exclusions_returns_empty_for_an_isin_with_none(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="SOME COMPANY")

    assert list_title_exclusions("GRS518003009", db_path=db_path) == []


def test_export_snapshot_round_trips_every_table(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS003003035", name="NAT. BANK OF GREECE SA")
    _seed_entity(db_path, lei="5UMCZOEYKCVFAW8ZLO05", legal_name="NATIONAL BANK OF GREECE S.A.")
    add_alias("GRS003003035", "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ", source="test", db_path=db_path)
    exclude_title_match("GRS003003035", "SOME UNRELATED TITLE", reason="test", db_path=db_path)

    snapshot = export_snapshot(db_path=db_path)

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.deserialize(snapshot)
    assert connection.execute("SELECT name FROM instruments").fetchone()["name"] == "NAT. BANK OF GREECE SA"
    assert connection.execute("SELECT legal_name FROM entities").fetchone()["legal_name"] == "NATIONAL BANK OF GREECE S.A."
    assert connection.execute("SELECT alias_text FROM instrument_aliases").fetchone()["alias_text"] == "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ"
    assert connection.execute("SELECT title_text FROM title_isin_exclusions").fetchone()["title_text"] == "some unrelated title"


def test_export_snapshot_reflects_a_write_made_after_the_snapshot_function_is_called_again(tmp_path):
    # Not stale/cached — a second call after a new write sees the new data.
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS003003035", name="OLD NAME")

    before = export_snapshot(db_path=db_path)
    conn = sqlite3.connect(":memory:")
    conn.deserialize(before)
    assert conn.execute("SELECT name FROM instruments").fetchone()[0] == "OLD NAME"

    _seed_instrument(db_path, isin="GRS831003009", name="A SECOND COMPANY")
    after = export_snapshot(db_path=db_path)
    conn2 = sqlite3.connect(":memory:")
    conn2.deserialize(after)
    assert conn2.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == 2


def test_import_snapshot_restores_every_table_into_an_empty_db(tmp_path):
    source_path = tmp_path / "source.db"
    _seed_instrument(source_path, isin="GRS003003035", name="NAT. BANK OF GREECE SA")
    _seed_entity(source_path, lei="5UMCZOEYKCVFAW8ZLO05", legal_name="NATIONAL BANK OF GREECE S.A.")
    add_alias("GRS003003035", "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ", source="test", db_path=source_path)
    exclude_title_match("GRS003003035", "SOME UNRELATED TITLE", reason="test", db_path=source_path)
    blacklist_lei("GRS003003035", "213800OYHR1MPQ5VJL60", db_path=source_path)
    snapshot = export_snapshot(db_path=source_path)

    target_path = tmp_path / "target.db"
    import_snapshot(snapshot, db_path=target_path)

    instrument = lookup_by_isin("GRS003003035", db_path=target_path)
    assert instrument is not None
    assert instrument.name == "NAT. BANK OF GREECE SA"
    entity = lookup_by_lei("5UMCZOEYKCVFAW8ZLO05", db_path=target_path)
    assert entity is not None
    connection = connect(target_path)
    aliases = connection.execute("SELECT alias_text FROM instrument_aliases").fetchall()
    exclusions = connection.execute("SELECT title_text FROM title_isin_exclusions").fetchall()
    blacklisted = connection.execute("SELECT isin, lei FROM lei_blacklist").fetchall()
    connection.close()
    assert [row["alias_text"] for row in aliases] == ["ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΑΕ"]
    assert [row["title_text"] for row in exclusions] == ["some unrelated title"]
    assert [(row["isin"], row["lei"]) for row in blacklisted] == [("GRS003003035", "213800OYHR1MPQ5VJL60")]


def test_import_snapshot_refuses_to_overwrite_a_nonempty_db_by_default(tmp_path):
    source_path = tmp_path / "source.db"
    _seed_instrument(source_path, isin="GRS003003035", name="NEW DATA")
    snapshot = export_snapshot(db_path=source_path)

    target_path = tmp_path / "target.db"
    _seed_instrument(target_path, isin="GRS831003009", name="EXISTING DATA, MUST SURVIVE")

    try:
        import_snapshot(snapshot, db_path=target_path)
        assert False, "expected ValueError"
    except ValueError:
        pass

    instrument = lookup_by_isin("GRS831003009", db_path=target_path)
    assert instrument is not None
    assert instrument.name == "EXISTING DATA, MUST SURVIVE"
    assert lookup_by_isin("GRS003003035", db_path=target_path) is None


def test_import_snapshot_overwrite_true_replaces_existing_data(tmp_path):
    source_path = tmp_path / "source.db"
    _seed_instrument(source_path, isin="GRS003003035", name="NEW DATA")
    snapshot = export_snapshot(db_path=source_path)

    target_path = tmp_path / "target.db"
    _seed_instrument(target_path, isin="GRS831003009", name="OLD DATA, SHOULD BE REPLACED")

    import_snapshot(snapshot, db_path=target_path, overwrite=True)

    assert lookup_by_isin("GRS831003009", db_path=target_path) is None
    instrument = lookup_by_isin("GRS003003035", db_path=target_path)
    assert instrument is not None
    assert instrument.name == "NEW DATA"


def _stock(isin, symbol="X"):
    return AthexStock(
        isin=isin, symbol=symbol, issuer=f"ISSUER {symbol}",
        issuer_full_name=f"ISSUER {symbol} S.A.", market="SECURITIES MARKET",
    )


def _feed_column(db_path, column="last_seen_in_feed"):
    connection = connect(db_path)
    try:
        return {
            row["isin"]: row[column]
            for row in connection.execute(f"SELECT isin, {column} FROM instruments")
        }
    finally:
        connection.close()


def _stocks_feed(monkeypatch, stocks):
    monkeypatch.setattr("instrument_registry.service.fetch_athex_stocks", lambda: stocks)


TEN = [f"GRS0000000{n:02d}" for n in range(10)]


def test_refresh_athex_stamps_what_it_saw_and_keeps_what_it_did_not(tmp_path, monkeypatch):
    # The point of #24: a company that leaves the feed keeps its row, its
    # ISIN and the date it was last listed.
    db_path = tmp_path / "registry.db"
    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN])
    refresh_athex(db_path=db_path)
    first = _feed_column(db_path)
    assert len(set(first.values())) == 1 and None not in first.values()

    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN[1:]])
    refresh_athex(db_path=db_path)

    second = _feed_column(db_path)
    assert second[TEN[0]] == first[TEN[0]]
    assert all(second[isin] > first[isin] for isin in TEN[1:])
    assert lookup_by_isin(TEN[0], db_path=db_path).last_seen_in_feed == first[TEN[0]]


def test_refresh_athex_never_judges_an_etf(tmp_path, monkeypatch):
    # The stocks feed contains no ETFs; reading their absence from it as a
    # delisting would be wrong for every ETF on every run.
    db_path = tmp_path / "registry.db"
    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_etfs",
        lambda: [AthexEtf(isin="GRF000153004", symbol="AETF", issuer="A", issuer_full_name="A")],
    )
    refresh_athex_etfs(db_path=db_path)
    etf_seen = _feed_column(db_path)["GRF000153004"]
    _stocks_feed(monkeypatch, [_stock(TEN[0])])

    refresh_athex(db_path=db_path)

    assert _feed_column(db_path)["GRF000153004"] == etf_seen
    assert _feed_column(db_path, "instrument_type")["GRF000153004"] == "etf"


def test_refresh_athex_refuses_an_empty_feed_and_writes_nothing(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN])
    refresh_athex(db_path=db_path)
    before = _feed_column(db_path)
    _stocks_feed(monkeypatch, [])

    with pytest.raises(FeedShrinkError, match="no rows"):
        refresh_athex(db_path=db_path)

    assert _feed_column(db_path) == before


def test_refresh_athex_refuses_an_empty_feed_even_into_an_empty_cache(tmp_path, monkeypatch):
    # No baseline to compare against, and still not an answer.
    _stocks_feed(monkeypatch, [])

    with pytest.raises(FeedShrinkError):
        refresh_athex(db_path=tmp_path / "registry.db")


def test_refresh_athex_refuses_a_partial_feed_and_writes_nothing(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN])
    refresh_athex(db_path=db_path)
    before = _feed_column(db_path)
    names_before = _feed_column(db_path, "name")
    _stocks_feed(monkeypatch, [_stock(isin, symbol="NEW") for isin in TEN[3:]])

    with pytest.raises(FeedShrinkError, match="omits 3 of the 10"):
        refresh_athex(db_path=db_path)

    assert _feed_column(db_path) == before
    assert _feed_column(db_path, "name") == names_before


def test_refresh_athex_allow_drop_accepts_a_confirmed_large_drop(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN])
    refresh_athex(db_path=db_path)
    before = _feed_column(db_path)
    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN[3:]])

    refresh_athex(db_path=db_path, allow_drop=True)

    after = _feed_column(db_path)
    assert [after[isin] == before[isin] for isin in TEN] == [True] * 3 + [False] * 7


def test_refresh_athex_counts_a_delisting_once(tmp_path, monkeypatch):
    # The baseline is what the previous run saw. Measured against every
    # row ever stored instead, the two already-gone stocks would count
    # again and push one ordinary delisting over the threshold.
    db_path = tmp_path / "registry.db"
    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN])
    refresh_athex(db_path=db_path)
    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN[2:]])
    refresh_athex(db_path=db_path)  # 2 of 10: at the threshold, allowed
    _stocks_feed(monkeypatch, [_stock(isin) for isin in TEN[3:]])

    refresh_athex(db_path=db_path)  # 1 of 8

    assert lookup_by_isin(TEN[2], db_path=db_path).last_seen_in_feed is not None


def test_refresh_athex_measures_a_pre_tracking_cache_against_every_stored_stock(
    tmp_path, monkeypatch
):
    # The first run after the migration has no stamps to compare against,
    # and a truncated payload then must still be refused.
    db_path = tmp_path / "registry.db"
    for isin in TEN:
        _seed_instrument(db_path, isin=isin, name=isin)
    _stocks_feed(monkeypatch, [_stock(TEN[0])])

    with pytest.raises(FeedShrinkError, match="omits 9 of the 10"):
        refresh_athex(db_path=db_path)


def test_refresh_athex_etfs_refuses_an_empty_feed(tmp_path, monkeypatch):
    # With one ETF listed, its delisting looks exactly like a failed fetch.
    db_path = tmp_path / "registry.db"
    monkeypatch.setattr(
        "instrument_registry.service.fetch_athex_etfs",
        lambda: [AthexEtf(isin="GRF000153004", symbol="AETF", issuer="A", issuer_full_name="A")],
    )
    refresh_athex_etfs(db_path=db_path)
    monkeypatch.setattr("instrument_registry.service.fetch_athex_etfs", lambda: [])

    with pytest.raises(FeedShrinkError):
        refresh_athex_etfs(db_path=db_path)


def test_connect_adds_last_seen_in_feed_to_an_existing_cache_and_says_so(tmp_path, capsys):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin=TEN[0], name="A")
    raw = sqlite3.connect(db_path)
    raw.execute("ALTER TABLE instruments DROP COLUMN last_seen_in_feed")
    raw.commit()
    raw.close()
    capsys.readouterr()

    connect(db_path).close()

    assert "instruments.last_seen_in_feed" in capsys.readouterr().err
    assert lookup_by_isin(TEN[0], db_path=db_path).last_seen_in_feed is None


def _backdate(db_path, table, isin):
    connection = connect(db_path)
    connection.execute(
        f"UPDATE {table} SET created_at = ? WHERE isin = ?", ("2026-01-01T00:00:00+00:00", isin)
    )
    connection.commit()
    connection.close()


def _created_at(db_path, table, isin):
    connection = connect(db_path)
    (value,) = connection.execute(
        f"SELECT created_at FROM {table} WHERE isin = ?", (isin,)
    ).fetchone()
    connection.close()
    return value


def test_add_alias_keeps_the_original_created_at_when_re_asserted(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS332003008", name="SOME COMPANY")
    add_alias("GRS332003008", "ALIAS ONE", source="first-pass", db_path=db_path)
    _backdate(db_path, "instrument_aliases", "GRS332003008")

    add_alias("GRS332003008", "ALIAS ONE", source="second-pass", confidence=0.9, db_path=db_path)

    [alias] = list_aliases("GRS332003008", db_path=db_path)
    assert alias.created_at == "2026-01-01T00:00:00+00:00"
    assert alias.source == "second-pass"
    assert alias.confidence == 0.9


def test_blacklist_lei_keeps_the_original_created_at_when_re_asserted(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="BG1100003166", name="MERMEREN KOMBINAT AD")
    blacklist_lei("BG1100003166", "M6AD1Y1KW32H8THQ6F76", reason="first", db_path=db_path)
    _backdate(db_path, "lei_blacklist", "BG1100003166")

    blacklist_lei("BG1100003166", "M6AD1Y1KW32H8THQ6F76", reason="second", db_path=db_path)

    [entry] = list_blacklisted(isin="BG1100003166", db_path=db_path)
    assert entry.created_at == "2026-01-01T00:00:00+00:00"
    assert entry.reason == "second"


def test_exclude_title_match_keeps_the_original_created_at_when_re_asserted(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS518003009", name="SOME COMPANY")
    exclude_title_match("GRS518003009", "SOME TITLE", reason="first", db_path=db_path)
    _backdate(db_path, "title_isin_exclusions", "GRS518003009")

    exclude_title_match("GRS518003009", "SOME TITLE", reason="second", db_path=db_path)

    [exclusion] = list_title_exclusions("GRS518003009", db_path=db_path)
    assert exclusion.created_at == "2026-01-01T00:00:00+00:00"
    assert exclusion.reason == "second"
