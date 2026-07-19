"""Look up a legal entity (LEI, ISO 17442) by one of its ISINs, via GLEIF's
free, no-auth, live search API.

GLEIF also publishes a full global "Golden Copy" bulk file (CC0), but for
Phase 1's actual need — resolving the LEI behind each of ATHEX's ~150
listed-stock ISINs — a targeted per-ISIN API call is simpler and avoids
pulling in and filtering a large global file for a mostly-irrelevant
99.99% of it. Confirmed live 2026-07-19: `filter[isin]=<ISIN>` against
`GRS003003035` correctly resolved to "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΤΗΣ ΕΛΛΑΔΟΣ Α.Ε."
(LEI 5UMCZOEYKCVFAW8ZLO05), with "NATIONAL BANK OF GREECE S.A." present
as a transliterated name — the original T20 motivating example.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

LEI_RECORDS_URL = "https://api.gleif.org/api/v1/lei-records"

_HEADERS = {"Accept": "application/vnd.api+json"}


@dataclass(frozen=True, slots=True)
class GleifEntity:
    lei: str
    legal_name: str
    other_names: list[str]
    country: str | None
    status: str | None


def lookup_lei_by_isin(isin: str, *, timeout: float = 15.0) -> GleifEntity | None:
    """One live GLEIF API call. Returns None if no entity is registered
    against this ISIN (common for smaller/older Greek issuers)."""
    with httpx.Client(headers=_HEADERS, timeout=timeout) as client:
        response = client.get(LEI_RECORDS_URL, params={"filter[isin]": isin})
        response.raise_for_status()
        payload = response.json()

    records = payload.get("data") or []
    if not records:
        return None

    entity = records[0]["attributes"]["entity"]
    other_names = [item["name"] for item in entity.get("otherNames", [])]
    other_names += [item["name"] for item in entity.get("transliteratedOtherNames", [])]

    return GleifEntity(
        lei=records[0]["attributes"]["lei"],
        legal_name=entity["legalName"]["name"],
        other_names=other_names,
        country=(entity.get("legalAddress") or {}).get("country"),
        status=entity.get("status"),
    )
