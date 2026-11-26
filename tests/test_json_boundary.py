"""The collectors' JSON boundary: a feed that changes shape must fail the
refresh loudly, not flow a wrong-typed value into the cache."""
from datetime import date

import pytest

from instrument_registry.collector._json import (
    FeedShapeError,
    as_object,
    as_objects,
    opt_str,
    req_int,
    req_str,
)
from instrument_registry.collector.athex import _issuer_full_name, _rows
from instrument_registry.collector.esma import _firds_bond


def test_req_str_rejects_a_field_that_turned_into_a_number():
    with pytest.raises(FeedShapeError, match=r"stocks\.data\[3\]\.ISIN: expected a string, got int"):
        req_str({"ISIN": 123}, "ISIN", "stocks.data[3]")


def test_a_missing_required_field_is_still_a_key_error():
    # Callers written against `row["X"]` expected KeyError; keep that true.
    with pytest.raises(KeyError, match="missing field 'Symbol'"):
        req_str({}, "Symbol", "row")


def test_opt_str_accepts_absent_and_null_but_not_another_type():
    assert opt_str({}, "Market", "row") is None
    assert opt_str({"Market": None}, "Market", "row") is None
    assert opt_str({"Market": "MAIN"}, "Market", "row") == "MAIN"
    with pytest.raises(FeedShapeError):
        opt_str({"Market": ["MAIN"]}, "Market", "row")


def test_req_int_rejects_a_boolean():
    assert req_int({"numFound": 7}, "numFound", "r") == 7
    with pytest.raises(FeedShapeError):
        req_int({"numFound": True}, "numFound", "r")


def test_as_objects_names_the_offending_index():
    with pytest.raises(FeedShapeError, match=r"data\[1\]: expected a JSON object, got str"):
        as_objects([{}, "oops"], "data")
    with pytest.raises(FeedShapeError, match="expected a JSON array"):
        as_objects({"not": "a list"}, "data")
    with pytest.raises(FeedShapeError, match="expected a JSON object"):
        as_object(None, "payload")


def test_athex_rows_reject_a_payload_without_a_data_array():
    with pytest.raises(FeedShapeError, match=r"stocks_en\.json\.data: expected a JSON array, got NoneType"):
        _rows({"rows": []}, "stocks_en.json")


def test_issuer_full_name_falls_back_to_issuer_when_absent_or_null():
    assert _issuer_full_name({"Issuer": "ALPHA"}, "r") == "ALPHA"
    assert _issuer_full_name({"Issuer": "ALPHA", "_issuerFullName": None}, "r") == "ALPHA"
    assert _issuer_full_name({"Issuer": "ALPHA", "_issuerFullName": "ALPHA BANK S.A."}, "r") == "ALPHA BANK S.A."


def test_firds_bond_parses_a_record_and_rejects_a_wrong_typed_one():
    doc = {
        "id": "x1", "isin": "GRC1451184D5", "gnr_full_name": "GGB1", "mic": "XATH",
        "gnr_cfi_code": "DBFTFB", "gnr_notional_curr_code": "EUR",
        "bnd_maturity_date": "2034-06-15T00:00:00Z", "status": None,
    }
    bond = _firds_bond(doc, "doc")
    assert bond.maturity == date(2034, 6, 15)
    assert bond.status is None
    assert _firds_bond({**doc, "bnd_maturity_date": None}, "doc").maturity is None
    with pytest.raises(FeedShapeError, match=r"doc\.mic"):
        _firds_bond({**doc, "mic": 1}, "doc")
