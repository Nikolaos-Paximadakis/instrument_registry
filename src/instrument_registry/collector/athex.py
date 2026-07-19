"""Fetch the Athens Stock Exchange (ATHEX) listed-stocks list.

`athens.euronext.com` sits behind Cloudflare bot protection that 403s a
plain/generic HTTP client (no browser headers, no HTTP/2) — confirmed
live 2026-07-19. A browser-header + HTTP/2 client gets a clean 200, the
same trick pothen_eshes.http_client already uses for
hellenicparliament.gr's Akamai protection. The instruments page itself
server-renders an empty Drupal Views table; the real data is a static
JSON file the page's own JS reads from
`/sites/default/files/json_data_files/{product_type}_{lang}.json`.

`Accept-Encoding` deliberately excludes `br` (Brotli): httpx can't
auto-decompress it without the optional `brotli`/`brotlicffi` package,
which this project doesn't depend on — gzip/deflate is enough.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

STOCKS_URL = "https://athens.euronext.com/sites/default/files/json_data_files/stocks_en.json"

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
            isin=row["ISIN"],
            symbol=row["Symbol"],
            issuer=row["Issuer"],
            issuer_full_name=row.get("_issuerFullName", row["Issuer"]),
            market=row.get("Market", ""),
        )
        for row in payload["data"]
    ]
