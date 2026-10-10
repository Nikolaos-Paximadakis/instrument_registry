"""Explicitly-run live test against ESMA's real FIRDS register, gated like
the other collectors' (see test_athex_collector.py). It also pins the
property the whole bond path rests on: ATHEX's tickers are findable in
FIRDS with an agreeing maturity date."""
import os

import pytest

from instrument_registry.collector.athex import fetch_athex_bonds
from instrument_registry.collector.esma import fetch_firds_athens_bonds
from instrument_registry.service import _resolve_bond_isins

pytestmark = pytest.mark.skipif(
    not os.environ.get("INSTRUMENT_REGISTRY_LIVE_TESTS"),
    reason="set INSTRUMENT_REGISTRY_LIVE_TESTS=1 to run live network tests",
)


def test_every_listed_athex_bond_resolves_to_one_isin_in_firds():
    athex = fetch_athex_bonds()
    firds = fetch_firds_athens_bonds()

    resolved, unresolved = _resolve_bond_isins(athex, firds)

    assert unresolved == [], "ATHEX bonds with no single FIRDS ISIN"
    assert len(resolved) == len(athex)
    assert len({bond.isin for bond in resolved}) == len(resolved)
    assert all(bond.isin[:2] in {"GR", "XS", "DE", "CY"} or bond.isin.isalnum() for bond in resolved)
