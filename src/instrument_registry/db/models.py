"""SQLite schema for the local instrument/entity cache.

Two tables, matched to the two real-world ISO identifiers this package
resolves against: LEI (ISO 17442, entity-level) and ISIN (ISO 6166,
instrument-level). See ~/Python/pothen_eshes/plans/T23-instrument-registry.md
for the reasoning.
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    lei TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL,
    other_names TEXT,
    country TEXT,
    status TEXT,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    isin TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    other_names TEXT,
    cfi_code TEXT,
    instrument_type TEXT,
    currency TEXT,
    lei TEXT REFERENCES entities(lei),
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instruments_lei ON instruments(lei);
CREATE INDEX IF NOT EXISTS idx_instruments_name ON instruments(name);
CREATE INDEX IF NOT EXISTS idx_entities_legal_name ON entities(legal_name);
"""


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()
