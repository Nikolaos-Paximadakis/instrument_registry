"""Fetch Athens Stock Exchange (ATHEX) listed-product lists.

`athens.euronext.com` sits behind Cloudflare bot protection that 403s a
plain/generic HTTP client (no browser headers, no HTTP/2) — confirmed
live 2026-07-19. A browser-header + HTTP/2 client gets a clean 200, the
same trick pothen_eshes.http_client already uses for
hellenicparliament.gr's Akamai protection. Each instruments page itself
server-renders an empty Drupal Views table; the real data is a static
JSON file the page's own JS reads from
`/sites/default/files/json_data_files/{product_type}_{lang}.json`.

`Accept-Encoding` deliberately excludes `br` (Brotli): httpx can't
auto-decompress it without the optional `brotli`/`brotlicffi` package,
which this project doesn't depend on — gzip/deflate is enough.

Confirmed live 2026-08-09 which sibling `{product_type}_en.json` feeds
exist and whether their shape actually fits this package's ISIN-keyed
schema — they don't all: `bonds_en.json` (59 rows, re-confirmed
2026-10-10) has no ISIN field at all, keyed by `_productId`/`Symbol`
instead, so `fetch_athex_bonds()` returns bonds without one and the ISIN
is resolved from ESMA FIRDS (`collector/esma.py`) in
`service.refresh_athex_bonds()`. `warrants_en.json` returned 0 rows live
on both dates, so its shape is unverified — nothing to fetch yet. `etfs_en.json` (1 row)
does carry ISIN and fits the same shape as stocks (modulo a `Market`
field stocks has and ETFs doesn't), hence `fetch_athex_etfs()` below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from instrument_registry.collector._json import (
    JsonObject,
    as_object,
    as_objects,
    opt_str,
    req_str,
)

STOCKS_URL = "https://athens.euronext.com/sites/default/files/json_data_files/stocks_en.json"
ETFS_URL = "https://athens.euronext.com/sites/default/files/json_data_files/etfs_en.json"
BONDS_URL = "https://athens.euronext.com/sites/default/files/json_data_files/bonds_en.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


@dataclass(frozen=True, slots=True)
class AthexStock:
    isin: str
    symbol: str
    issuer: str
    issuer_full_name: str
    market: str


def fetch_athex_stocks(*, timeout: float = 30.0) -> list[AthexStock]:
    """Fetch ATHEX's current listed-stocks list. One real HTTP call, no cache."""
    with httpx.Client(http2=True, headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
        response = client.get(STOCKS_URL)
        response.raise_for_status()
        payload = response.json()

    return [
        AthexStock(
            isin=req_str(row, "ISIN", where),
            symbol=req_str(row, "Symbol", where),
            issuer=req_str(row, "Issuer", where),
            issuer_full_name=_issuer_full_name(row, where),
            market=opt_str(row, "Market", where) or "",
        )
        for where, row in _rows(payload, "stocks_en.json")
    ]


@dataclass(frozen=True, slots=True)
class AthexEtf:
    isin: str
    symbol: str
    issuer: str
    issuer_full_name: str


def fetch_athex_etfs(*, timeout: float = 30.0) -> list[AthexEtf]:
    """Fetch ATHEX's current listed-ETFs list. Same JSON-file-behind-
    Cloudflare approach as `fetch_athex_stocks()`, a sibling product-type
    endpoint (`etfs_en.json` vs `stocks_en.json`). Only one ETF was
    listed live as of 2026-08-09 — a materially smaller, differently-
    shaped feed than stocks (no `Market` field), but the same one real
    field that matters for this package: `ISIN` is present, unlike
    `bonds_en.json`."""
    with httpx.Client(http2=True, headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
        response = client.get(ETFS_URL)
        response.raise_for_status()
        payload = response.json()

    return [
        AthexEtf(
            isin=req_str(row, "ISIN", where),
            symbol=req_str(row, "Symbol", where),
            issuer=req_str(row, "Issuer", where),
            issuer_full_name=_issuer_full_name(row, where),
        )
        for where, row in _rows(payload, "etfs_en.json")
    ]


@dataclass(frozen=True, slots=True)
class AthexBond:
    """A listed bond as ATHEX publishes it — which is *without an ISIN*.
    `collector/esma.py` is where the ISIN comes from; see
    `service.refresh_athex_bonds()` for the join."""
    symbol: str
    issuer: str
    issuer_full_name: str
    maturity: date


def fetch_athex_bonds(*, timeout: float = 30.0) -> list[AthexBond]:
    """Fetch ATHEX's current listed-bonds list (`bonds_en.json`, 59 rows
    live 2026-10-10). Unlike stocks and ETFs this feed carries no ISIN —
    it is keyed by `_productId`/`Symbol` — so a row is only half of an
    instrument until the ISIN is resolved elsewhere. `Maturity` is kept
    because it is what lets that resolution be checked rather than trusted.
    Covers both ATHEX markets (`ib_market` ATH and ENAX)."""
    with httpx.Client(http2=True, headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
        response = client.get(BONDS_URL)
        response.raise_for_status()
        payload = response.json()

    return [
        AthexBond(
            symbol=req_str(row, "Symbol", where),
            issuer=req_str(row, "Issuer", where),
            issuer_full_name=_issuer_full_name(row, where),
            maturity=date.fromisoformat(
                req_str(as_object(row.get("Maturity"), f"{where}.Maturity"), "date", where)[:10]
            ),
        )
        for where, row in _rows(payload, "bonds_en.json")
    ]


def _rows(payload: object, feed: str) -> list[tuple[str, JsonObject]]:
    """The feed's `data` rows, each paired with a location for error messages."""
    rows = as_objects(as_object(payload, feed).get("data"), f"{feed}.data")
    return [(f"{feed}.data[{i}]", row) for i, row in enumerate(rows)]


def _issuer_full_name(row: JsonObject, where: str) -> str:
    # Falls back to `Issuer` when the field is absent, as before, and now
    # also when it is null rather than storing None in a `str` field.
    full_name = opt_str(row, "_issuerFullName", where)
    return full_name if full_name is not None else req_str(row, "Issuer", where)
