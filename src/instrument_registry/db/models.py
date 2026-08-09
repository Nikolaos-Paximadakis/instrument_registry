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
    updated_at TEXT NOT NULL,
    symbol TEXT
);

CREATE INDEX IF NOT EXISTS idx_instruments_lei ON instruments(lei);
CREATE INDEX IF NOT EXISTS idx_instruments_name ON instruments(name);
CREATE INDEX IF NOT EXISTS idx_entities_legal_name ON entities(legal_name);

-- Locally-learned alternate spellings of an instrument, e.g. harvested from
-- a consuming project's own confirmed human/AI title-merge decisions
-- (pothen_eshes.title_review). Deliberately separate from `instruments`
-- itself: refresh_athex()/refresh_gleif() only ever INSERT/UPDATE those two
-- source-of-truth tables and never touch this one, so a refresh can never
-- wipe a learned alias. Consulted by fuzzy_match_title_scored() as an extra
-- candidate string per instrument (an exact alias hit naturally scores
-- ratio 1.0 via the same difflib comparison, no separate fast-path branch
-- needed) — the payoff is a previously-unseen-but-already-reconciled title
-- string resolving to its instrument immediately next time it's matched.
CREATE TABLE IF NOT EXISTS instrument_aliases (
    alias_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    isin        TEXT NOT NULL REFERENCES instruments(isin),
    alias_text  TEXT NOT NULL,
    source      TEXT NOT NULL,
    confidence  REAL,
    created_at  TEXT NOT NULL,
    UNIQUE (isin, alias_text)
);

CREATE INDEX IF NOT EXISTS idx_instrument_aliases_isin ON instrument_aliases(isin);

-- ISIN→LEI links that GLEIF's own API reports but that are known to be wrong.
-- refresh_gleif() consults this before writing a link, so a bad linkage stays
-- corrected instead of silently reappearing on the next run (it only re-queries
-- instruments with `lei IS NULL`, so a hand-nulled row is exactly the row it
-- retries). Same reasoning as instrument_aliases: local knowledge that the
-- upstream source doesn't have, kept in its own table so a refresh can't wipe
-- it. Keyed on the pair, not the ISIN alone — if GLEIF later returns a
-- *different*, correct LEI for that ISIN, it still links normally.
CREATE TABLE IF NOT EXISTS lei_blacklist (
    isin       TEXT NOT NULL,
    lei        TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (isin, lei)
);

-- The inverse of instrument_aliases: a specific title string that must
-- NEVER be considered a match for a specific ISIN, even if its computed
-- ratio would otherwise clear a caller's threshold. Exists because generic
-- corporate-boilerplate overlap (e.g. a shared "ΣΥΜΜΕΤΟΧΩΝ Α.Ε."/"Holding
-- S.A." suffix) can produce a plausible-looking ratio between two
-- genuinely unrelated companies (confirmed live: "QUEST ΣΥΜΜΕΤΟΧΩΝ Α.Ε."
-- scored 73% against the unrelated "ADMIE (IPTO) HOLDING S.A.", ahead of
-- the real match, "QUEST HOLDINGS S.A.", at 72%) — without an exclusion,
-- every future match against that exact title (including a re-run of a
-- consumer's own batch matching job) would keep re-deriving the identical
-- wrong top candidate, since the scoring is deterministic. Same
-- upstream-can't-touch-it reasoning as instrument_aliases/lei_blacklist:
-- refresh_athex()/refresh_gleif() never write here. title_text is stored
-- normalized (stripped + casefolded), matching how fuzzy_match_title_scored()
-- itself normalizes before comparing.
CREATE TABLE IF NOT EXISTS title_isin_exclusions (
    isin       TEXT NOT NULL REFERENCES instruments(isin),
    title_text TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (isin, title_text)
);
"""


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    _migrate(connection)
    connection.commit()


def _migrate(connection: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` only handles a table that doesn't
    exist yet — it can't add a column to an `instruments` table an
    already-deployed cache created before that column existed. Each
    migration here is guarded by checking `PRAGMA table_info` first, so
    it's a no-op (not an error) on a DB that already has the column,
    letting this run unconditionally on every `connect()` like the rest
    of `create_schema()`. `idx_instruments_symbol` is created here
    rather than in `SCHEMA` itself, since `SCHEMA` runs first and would
    otherwise try to index a column that, on an existing pre-migration
    DB, doesn't exist yet."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(instruments)")}
    if "symbol" not in columns:
        connection.execute("ALTER TABLE instruments ADD COLUMN symbol TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments(symbol)"
    )
