"""Explicitly-run live test against the real ATHEX source (not mocked).
Gated behind an env var so a normal `uv run pytest` never hits the
network — same "live-verify-don't-just-mock" discipline pothen_eshes
follows for its own external-API tests."""
import os

import pytest

from instrument_registry.collector.athex import fetch_athex_stocks

pytestmark = pytest.mark.skipif(
    not os.environ.get("INSTRUMENT_REGISTRY_LIVE_TESTS"),
    reason="set INSTRUMENT_REGISTRY_LIVE_TESTS=1 to run live network tests",
)


def test_fetch_athex_stocks_returns_real_listed_stocks():
    stocks = fetch_athex_stocks()

    assert len(stocks) > 100
    isins = {stock.isin for stock in stocks}
    assert len(isins) == len(stocks), "expected every ISIN to be unique"

    national_bank = next(stock for stock in stocks if stock.symbol == "ETE")
    assert "NAT" in national_bank.issuer.upper() or "GREECE" in national_bank.issuer.upper()
    assert national_bank.isin.startswith("GR")
