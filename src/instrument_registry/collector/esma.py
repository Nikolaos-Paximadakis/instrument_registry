"""Fetch debt instruments admitted to Athens venues from ESMA's FIRDS.

FIRDS (Financial Instruments Reference Data System) is the EU register of
every instrument admitted to trading on an EU venue: ISIN, venue MIC, CFI,
currency, maturity. It exists here because ATHEX's `bonds_en.json` has no
ISIN (see `collector/athex.py`), and matching bonds to ISINs by name would
bring back the fuzzy matching this package exists to replace.

Queried through the Solr backend behind `registers.esma.europa.eu`. That
is the register's own web-UI endpoint, not a documented API, so it can
change without notice — the live test is what notices. No Cloudflare
protection, plain HTTP works.

Confirmed live 2026-10-10: for these instruments FIRDS's `gnr_full_name`
is the ATHEX ticker (`ROENB1`, `GEKTERNAB4`), so it joins to ATHEX's
`Symbol` exactly. All 59 listed bonds resolved to one ISIN each, with
`bnd_maturity_date` agreeing with ATHEX's `Maturity`. Two things that bit:
 - a bond on ENAX (EN.A. Growth) is not on `XATH`, so filtering on XATH
   alone silently loses it (`ROENB1`);
 - one instrument can have several FIRDS records (same ISIN), and
   terminated ones stay in the register, so callers must dedupe by ISIN
   and ignore `status == "TERM"`.
FIRDS also returns the issuer LEI; it is deliberately not read here —
`refresh_gleif()` owns LEI linking, its blacklist and the `entities` row
the link points at.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date

import httpx

FIRDS_URL = "https://registers.esma.europa.eu/solr/esma_registers_firds/select"

#: ATHEX's two markets: the main one and EN.A. Growth.
ATHENS_MICS = ("XATH", "ENAX")

_PAGE_SIZE = 500

#: The register's gateway answers a cold query with a 504 now and then
#: (seen live 2026-10-10: 20s, then fine on the immediate retry).
_ATTEMPTS = 3
_RETRY_DELAY = 2.0


@dataclass(frozen=True, slots=True)
class FirdsBond:
    isin: str
    symbol: str
    mic: str
    cfi_code: str | None
    currency: str | None
    maturity: date | None
    status: str | None


def fetch_firds_athens_bonds(*, timeout: float = 60.0) -> list[FirdsBond]:
    """Every FIRDS debt-instrument record (CFI `D*`) on an Athens venue,
    terminated ones included (`status` says which). One record per FIRDS
    row, so the same ISIN can appear more than once."""
    params = {
        "q": "*",
        "fq": [f"mic:({' OR '.join(ATHENS_MICS)})", "gnr_cfi_code:D*"],
        "wt": "json",
        # No `sort`: ~200 rows fit in one page, and `sort=id asc` timed out
        # (504 after 20s) on 1 of 4 live calls where the unsorted query
        # never did.
        "rows": _PAGE_SIZE,
    }
    docs: list[dict] = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        while True:
            result = _get_page(client, {**params, "start": len(docs)})
            docs.extend(result["docs"])
            if not result["docs"] or len(docs) >= result["numFound"]:
                break

    return [
        FirdsBond(
            isin=doc["isin"],
            symbol=doc["gnr_full_name"],
            mic=doc["mic"],
            cfi_code=doc.get("gnr_cfi_code"),
            currency=doc.get("gnr_notional_curr_code"),
            maturity=(
                date.fromisoformat(doc["bnd_maturity_date"][:10])
                if doc.get("bnd_maturity_date") else None
            ),
            status=doc.get("status"),
        )
        for doc in docs
    ]


def _get_page(client: httpx.Client, params: dict) -> dict:
    """One page, retried on a 5xx or a transport failure. A 4xx is a bad
    query, not a flaky gateway, and is raised at once."""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            response = client.get(FIRDS_URL, params=params)
            response.raise_for_status()
            return response.json()["response"]
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            retryable = isinstance(error, httpx.TransportError) or error.response.status_code >= 500
            if not retryable or attempt == _ATTEMPTS:
                raise
            time.sleep(_RETRY_DELAY)
