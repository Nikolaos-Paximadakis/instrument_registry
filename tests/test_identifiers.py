"""ISIN (ISO 6166) and LEI (ISO 17442) validation, and the write functions
that refuse a typo'd or swapped identifier before it reaches a learned
table nothing can regenerate."""
import pytest

from instrument_registry import (
    add_alias,
    blacklist_lei,
    exclude_title_match,
    remove_alias,
)
from instrument_registry.db.session import connect
from instrument_registry.identifiers import (
    is_valid_isin,
    is_valid_lei,
    parse_isin,
    parse_lei,
)
from instrument_registry.service import (
    remove_title_exclusion,
    unblacklist_lei,
)

# Real identifiers: National Bank of Greece's ISIN and LEI (see gleif.py).
NBG_ISIN = "GRS003003035"
NBG_LEI = "5UMCZOEYKCVFAW8ZLO05"


@pytest.mark.parametrize("value", [NBG_ISIN, "US0378331005", "GRF000153004"])
def test_valid_isins(value):
    assert is_valid_isin(value)
    assert parse_isin(value) == value


@pytest.mark.parametrize("value", [
    "GRS003003036",       # wrong check digit
    "GRS00300303",        # too short
    "grs003003035",       # lower case is refused, not normalised
    " GRS003003035",      # so is whitespace
    "1RS003003035",       # country code must be letters
    NBG_LEI,              # an LEI is not an ISIN
    "",
])
def test_invalid_isins(value):
    assert not is_valid_isin(value)
    with pytest.raises(ValueError, match="not a valid ISIN"):
        parse_isin(value)


def test_valid_and_invalid_leis():
    assert is_valid_lei(NBG_LEI)
    assert parse_lei(NBG_LEI) == NBG_LEI
    for bad in ("5UMCZOEYKCVFAW8ZLO06", NBG_ISIN, "5umczoeykcvfaw8zlo05", ""):
        assert not is_valid_lei(bad)
        with pytest.raises(ValueError, match="not a valid LEI"):
            parse_lei(bad)


def _cache_with_nbg(tmp_path):
    db_path = tmp_path / "registry.db"
    connection = connect(db_path)
    connection.execute(
        "INSERT INTO instruments (isin, name, other_names, instrument_type, source, updated_at) "
        "VALUES (?, 'NAT. BANK OF GREECE SA', '[]', 'stock', 'athex', '2026-01-01T00:00:00+00:00')",
        (NBG_ISIN,),
    )
    connection.commit()
    connection.close()
    return db_path


def _learned_row_count(db_path):
    connection = connect(db_path)
    try:
        return sum(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("instrument_aliases", "lei_blacklist", "title_isin_exclusions",
                          "learned_tombstones")
        )
    finally:
        connection.close()


def test_swapped_isin_and_lei_are_refused_by_blacklist_lei(tmp_path):
    db_path = _cache_with_nbg(tmp_path)
    with pytest.raises(ValueError, match="not a valid ISIN"):
        blacklist_lei(NBG_LEI, NBG_ISIN, db_path=db_path)
    assert _learned_row_count(db_path) == 0


def test_every_learned_write_refuses_a_malformed_identifier(tmp_path):
    db_path = _cache_with_nbg(tmp_path)
    typo = "GRS003003036"
    calls = [
        lambda: add_alias(typo, "ΕΘΝΙΚΗ", source="test", db_path=db_path),
        lambda: exclude_title_match(typo, "ΤΙΤΛΟΣ", db_path=db_path),
        lambda: blacklist_lei(typo, NBG_LEI, db_path=db_path),
        lambda: blacklist_lei(NBG_ISIN, "5UMCZOEYKCVFAW8ZLO06", db_path=db_path),
        # Removals write a tombstone, so they validate too.
        lambda: remove_alias(typo, "ΕΘΝΙΚΗ", db_path=db_path),
        lambda: remove_title_exclusion(typo, "ΤΙΤΛΟΣ", db_path=db_path),
        lambda: unblacklist_lei(typo, NBG_LEI, db_path=db_path),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="not a valid"):
            call()
    assert _learned_row_count(db_path) == 0
