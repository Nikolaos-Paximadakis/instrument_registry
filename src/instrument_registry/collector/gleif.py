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

from instrument_registry.collector._json import as_object, as_objects, opt_str, req_str

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

    where = f"GLEIF lei-records[isin={isin}]"
    records = as_objects(as_object(payload, where).get("data") or [], f"{where}.data")
    if not records:
        return None

    attributes = as_object(records[0].get("attributes"), f"{where}.data[0].attributes")
    entity = as_object(attributes.get("entity"), f"{where}.entity")
    other_names = [
        req_str(item, "name", f"{where}.entity.{key}")
        for key in ("otherNames", "transliteratedOtherNames")
        for item in as_objects(entity.get(key) or [], f"{where}.entity.{key}")
    ]
    address = entity.get("legalAddress")

    return GleifEntity(
        lei=req_str(attributes, "lei", where),
        legal_name=req_str(as_object(entity.get("legalName"), f"{where}.entity.legalName"), "name", where),
        other_names=other_names,
        country=opt_str(as_object(address, f"{where}.entity.legalAddress"), "country", where) if address else None,
        status=opt_str(entity, "status", where),
    )
