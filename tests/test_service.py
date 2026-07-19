from __future__ import annotations

from instrument_registry.collector.athex import AthexStock
from instrument_registry.collector.gleif import GleifEntity
from instrument_registry.db.session import connect
from instrument_registry.service import (
    fuzzy_match_title,
    lookup_by_isin,
    lookup_by_lei,
    refresh_athex,
    refresh_gleif,
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


def test_fuzzy_match_title_ranks_by_similarity(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS003003035", name="NATIONAL BANK OF GREECE S.A.")
    _seed_instrument(db_path, isin="GRS831003009", name="PIRAEUS BANK S.A.")

    matches = fuzzy_match_title("NATIONAL BANK OF GREECE", threshold=0.6, db_path=db_path)

    assert [m.isin for m in matches] == ["GRS003003035"]


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

    linked = refresh_gleif(db_path=db_path)

    assert linked == 1
    instrument = lookup_by_isin("GRS003003035", db_path=db_path)
    assert instrument.lei == "5UMCZOEYKCVFAW8ZLO05"
    entity = lookup_by_lei("5UMCZOEYKCVFAW8ZLO05", db_path=db_path)
    assert entity.legal_name == "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΤΗΣ ΕΛΛΑΔΟΣ Α.Ε."
    assert "NATIONAL BANK OF GREECE S.A." in entity.other_names


def test_refresh_gleif_leaves_isin_unlinked_when_no_entity_found(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    _seed_instrument(db_path, isin="GRS999999999", name="SOME OLD ISSUER")

    monkeypatch.setattr(
        "instrument_registry.service.lookup_lei_by_isin", lambda isin: None
    )

    linked = refresh_gleif(db_path=db_path)

    assert linked == 0
    assert lookup_by_isin("GRS999999999", db_path=db_path).lei is None
