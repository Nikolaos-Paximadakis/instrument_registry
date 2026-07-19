"""Explicitly-run live test against the real GLEIF API (not mocked)."""
import os

import pytest

from instrument_registry.collector.gleif import lookup_lei_by_isin

pytestmark = pytest.mark.skipif(
    not os.environ.get("INSTRUMENT_REGISTRY_LIVE_TESTS"),
    reason="set INSTRUMENT_REGISTRY_LIVE_TESTS=1 to run live network tests",
)


def test_lookup_lei_by_isin_resolves_national_bank_of_greece():
    entity = lookup_lei_by_isin("GRS003003035")

    assert entity is not None
    assert entity.lei == "5UMCZOEYKCVFAW8ZLO05"
    assert any("NATIONAL BANK OF GREECE" in name.upper() for name in entity.other_names)


def test_lookup_lei_by_isin_returns_none_for_a_made_up_isin():
    entity = lookup_lei_by_isin("XX0000000000")

    assert entity is None
