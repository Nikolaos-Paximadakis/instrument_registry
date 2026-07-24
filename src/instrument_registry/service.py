"""Public API. Read-only lookups query the local SQLite cache only — no
live network call per lookup (fetch once via refresh_*, cache, query
fast). `refresh_athex()`/`refresh_gleif()` are the only functions that
hit the network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

from instrument_registry.collector.athex import fetch_athex_stocks
from instrument_registry.collector.gleif import lookup_lei_by_isin
from instrument_registry.db.session import connect


@dataclass(frozen=True, slots=True)
class Instrument:
    isin: str
    name: str
    other_names: list[str]
    cfi_code: str | None
    instrument_type: str | None
    currency: str | None
    lei: str | None
    source: str


@dataclass(frozen=True, slots=True)
class Entity:
    lei: str
    legal_name: str
    other_names: list[str]
    country: str | None
    status: str | None
    source: str


@dataclass(frozen=True, slots=True)
class GleifRefreshResult:
    """What a `refresh_gleif()` run did. `linked` alone can't distinguish a
    run that found nothing from one that suppressed a known-bad link, which
    is exactly the case worth noticing."""
    linked: int
    skipped_blacklisted: int


def refresh_athex(*, db_path: str | Path | None = None) -> int:
    """Fetch ATHEX's current listed-stocks list and upsert into
    `instruments`. Safe to re-run: an existing row's `lei`/`cfi_code`/
    `currency` (filled in by a later step, e.g. `refresh_gleif()`) is
    never clobbered back to NULL, only `name`/`other_names` refresh.
    ATHEX's own JSON has none of those three fields — this source is
    stocks-only, so `instrument_type` is hardcoded to 'stock' rather than
    left NULL."""
    stocks = fetch_athex_stocks()
    now = datetime.now(UTC).isoformat()
    connection = connect(db_path)
    try:
        for stock in stocks:
            other_names = sorted(
                {stock.symbol, stock.issuer_full_name} - {stock.issuer}
            )
            connection.execute(
                """
                INSERT INTO instruments (
                    isin, name, other_names, instrument_type, source, updated_at
                ) VALUES (?, ?, ?, 'stock', 'athex', ?)
                ON CONFLICT(isin) DO UPDATE SET
                    name = excluded.name,
                    other_names = excluded.other_names,
                    instrument_type = excluded.instrument_type,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (stock.isin, stock.issuer, json.dumps(other_names, ensure_ascii=False), now),
            )
        connection.commit()
    finally:
        connection.close()
    return len(stocks)


def refresh_gleif(*, db_path: str | Path | None = None) -> GleifRefreshResult:
    """For every cached instrument missing a `lei`, look it up by ISIN
    against GLEIF's live API and link it. Returns a `GleifRefreshResult`
    counting instruments newly linked (an ISIN with no registered LEI,
    common for smaller/older Greek issuers, is left NULL and retried next
    run) and pairs suppressed as blacklisted.

    Any (isin, lei) pair recorded via `blacklist_lei()` is looked up but
    not written — the ISIN is left NULL and neither the link nor the
    entity row is created. The lookup still happens because the pair, not
    the ISIN, is what's blacklisted: if GLEIF later returns a different,
    correct LEI for that ISIN, it links normally. That costs one wasted
    request per blacklisted ISIN per run, which is the price of picking
    up an upstream fix automatically."""
    now = datetime.now(UTC).isoformat()
    connection = connect(db_path)
    linked = 0
    skipped_blacklisted = 0
    try:
        pending_isins = [
            row["isin"]
            for row in connection.execute(
                "SELECT isin FROM instruments WHERE lei IS NULL"
            ).fetchall()
        ]
        blacklisted = {
            (row["isin"], row["lei"])
            for row in connection.execute("SELECT isin, lei FROM lei_blacklist").fetchall()
        }
        for isin in pending_isins:
            entity = lookup_lei_by_isin(isin)
            if entity is None:
                continue
            if (isin, entity.lei) in blacklisted:
                skipped_blacklisted += 1
                continue
            connection.execute(
                """
                INSERT INTO entities (
                    lei, legal_name, other_names, country, status, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'gleif', ?)
                ON CONFLICT(lei) DO UPDATE SET
                    legal_name = excluded.legal_name,
                    other_names = excluded.other_names,
                    country = excluded.country,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    entity.lei,
                    entity.legal_name,
                    json.dumps(entity.other_names, ensure_ascii=False),
                    entity.country,
                    entity.status,
                    now,
                ),
            )
            connection.execute(
                "UPDATE instruments SET lei = ?, updated_at = ? WHERE isin = ?",
                (entity.lei, now, isin),
            )
            linked += 1
        connection.commit()
    finally:
        connection.close()
    return GleifRefreshResult(linked=linked, skipped_blacklisted=skipped_blacklisted)


def add_alias(
    isin: str,
    alias_text: str,
    *,
    source: str,
    confidence: float | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Records `alias_text` as a known alternate spelling of `isin`, learned
    outside ATHEX/GLEIF (e.g. a consuming project's own confirmed human/AI
    title-merge decision) — `source` should identify where it came from
    (e.g. "pothen_eshes.title_review:cluster_id=366"). Upserts on
    (isin, alias_text), so re-harvesting the same decision refreshes rather
    than duplicates. Silently a no-op for an unknown isin (FK constraint —
    callers are expected to have already resolved the isin via a match)."""
    now = datetime.now(UTC).isoformat()
    connection = connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO instrument_aliases (isin, alias_text, source, confidence, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(isin, alias_text) DO UPDATE SET
                source = excluded.source,
                confidence = excluded.confidence,
                created_at = excluded.created_at
            """,
            (isin, alias_text, source, confidence, now),
        )
        connection.commit()
    finally:
        connection.close()


def blacklist_lei(
    isin: str,
    lei: str,
    *,
    reason: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Records that GLEIF's `isin`→`lei` link is known-wrong, so
    `refresh_gleif()` stops re-applying it, and clears the link if it's
    already been written. `reason` should say how it was established
    (e.g. "GLEIF returns Piraeus Bank's LEI for Mermeren Kombinat's
    ISIN; confirmed against a raw live query 2026-07-19").

    Only the instrument's own link is cleared — the entity row is left
    alone, since a wrongly-linked LEI is usually a perfectly real entity
    that other instruments legitimately point at."""
    now = datetime.now(UTC).isoformat()
    connection = connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO lei_blacklist (isin, lei, reason, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(isin, lei) DO UPDATE SET
                reason = excluded.reason,
                created_at = excluded.created_at
            """,
            (isin, lei, reason, now),
        )
        connection.execute(
            "UPDATE instruments SET lei = NULL, updated_at = ? WHERE isin = ? AND lei = ?",
            (now, isin, lei),
        )
        connection.commit()
    finally:
        connection.close()


def lookup_by_isin(isin: str, *, db_path: str | Path | None = None) -> Instrument | None:
    connection = connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM instruments WHERE isin = ?", (isin,)
        ).fetchone()
    finally:
        connection.close()
    return _row_to_instrument(row) if row is not None else None


def lookup_by_lei(lei: str, *, db_path: str | Path | None = None) -> Entity | None:
    connection = connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM entities WHERE lei = ?", (lei,)
        ).fetchone()
    finally:
        connection.close()
    return _row_to_entity(row) if row is not None else None


def fuzzy_match_title(
    title: str,
    *,
    instrument_type: str | None = None,
    threshold: float = 0.75,
    db_path: str | Path | None = None,
) -> list[Instrument]:
    """Rank cached instruments by string similarity to `title` (same
    difflib-ratio approach pothen_eshes.title_review already uses against
    pending titles, just against real reference data instead). Advisory
    only — returns ranked candidates, decides nothing.

    Matches against the instrument's own name/other_names *and*, when
    it's linked to an entity, that entity's legal_name/other_names too —
    ATHEX's own data is English-only, but GLEIF's entity record often
    carries the Greek legal name (e.g. "ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ ΤΗΣ ΕΛΛΑΔΟΣ Α.Ε."
    for National Bank of Greece), and pothen_eshes titles are frequently
    Greek-script.

    See `fuzzy_match_title_scored()` for a variant that also returns each
    match's ratio — a caller recomputing its own ratio against only
    `instrument.name` would get a wrong (too-low) number for any match
    that actually came from `other_names` or the linked entity's names.
    """
    return [instrument for _, instrument in fuzzy_match_title_scored(
        title, instrument_type=instrument_type, threshold=threshold, db_path=db_path)]


def fuzzy_match_title_scored(
    title: str,
    *,
    instrument_type: str | None = None,
    threshold: float = 0.75,
    db_path: str | Path | None = None,
) -> list[tuple[float, Instrument]]:
    """Same matching as `fuzzy_match_title()`, but also returns each
    match's actual best ratio (against whichever candidate string —
    name, other_names, a linked entity's names, or a learned alias —
    produced it), for a caller that wants to judge or display match
    confidence rather than just take the ranked list."""
    connection = connect(db_path)
    try:
        if instrument_type is not None:
            rows = connection.execute(
                """
                SELECT instruments.*, entities.legal_name AS entity_legal_name,
                       entities.other_names AS entity_other_names
                FROM instruments
                LEFT JOIN entities ON entities.lei = instruments.lei
                WHERE instruments.instrument_type = ?
                """,
                (instrument_type,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT instruments.*, entities.legal_name AS entity_legal_name,
                       entities.other_names AS entity_other_names
                FROM instruments
                LEFT JOIN entities ON entities.lei = instruments.lei
                """
            ).fetchall()
        aliases_by_isin: dict[str, list[str]] = {}
        for alias_row in connection.execute("SELECT isin, alias_text FROM instrument_aliases").fetchall():
            aliases_by_isin.setdefault(alias_row["isin"], []).append(alias_row["alias_text"])
    finally:
        connection.close()

    normalized_title = title.strip().casefold()
    scored: list[tuple[float, Instrument]] = []
    for row in rows:
        instrument = _row_to_instrument(row)
        candidates = [instrument.name, *instrument.other_names, *aliases_by_isin.get(instrument.isin, [])]
        if row["entity_legal_name"] is not None:
            candidates.append(row["entity_legal_name"])
        if row["entity_other_names"]:
            candidates.extend(json.loads(row["entity_other_names"]))
        best_ratio = max(
            SequenceMatcher(None, normalized_title, candidate.strip().casefold()).ratio()
            for candidate in candidates
        )
        if best_ratio >= threshold:
            scored.append((best_ratio, instrument))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def _row_to_instrument(row) -> Instrument:
    return Instrument(
        isin=row["isin"],
        name=row["name"],
        other_names=json.loads(row["other_names"]) if row["other_names"] else [],
        cfi_code=row["cfi_code"],
        instrument_type=row["instrument_type"],
        currency=row["currency"],
        lei=row["lei"],
        source=row["source"],
    )


def _row_to_entity(row) -> Entity:
    return Entity(
        lei=row["lei"],
        legal_name=row["legal_name"],
        other_names=json.loads(row["other_names"]) if row["other_names"] else [],
        country=row["country"],
        status=row["status"],
        source=row["source"],
    )
