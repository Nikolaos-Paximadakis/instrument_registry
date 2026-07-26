# instrument_registry

`instrument_registry` is a shared reference-data service for real-world
financial instruments (ISIN, ISO 6166) and the legal entities that issue
them (LEI, ISO 17442). It exists so stock-related projects — starting
with `pothen_eshes` — can resolve "is this the same real company/stock?"
against actual reference data instead of fuzzy-matching free-text titles
against each other forever.

## Why this exists

`pothen_eshes` dedupes near-duplicate security titles by string
similarity (`difflib`). That catches spelling variants
(`"...S.A"` vs `"...S.A."`) but not two genuinely different-looking
titles for the same real company — e.g. `ΕΘΝΙΚΗ ΤΡΑΠΕΖΑ`/`NATIONAL BANK
OF GREECE`, or `TITAN CEMENT INTERNATIONAL S.A`/`TITAN S.A. (ΚΑ)`. An
ISIN identifies one specific instrument; an LEI identifies the issuing
entity independent of any one instrument — so a stock and a bond from the
same company share an LEI even though their ISINs differ. Anchoring to
these gives a standards-based way to resolve "same company, different
wording/instrument" instead of relying on string similarity forever.

## Phase 1 scope

Greek stocks only, proven end-to-end, before expanding to other
instrument types (mutual funds, bonds — a materially harder sourcing
problem, no single free public registry the way ATHEX/GLEIF are for
stocks/entities).

## What It Owns

- Fetching ATHEX's current listed-stocks list (ISIN, symbol, issuer name)
- Looking up the LEI/legal entity behind an instrument's ISIN, via GLEIF's
  free live API
- Local SQLite cache of both (`entities` + `instruments`), plus local
  knowledge the upstream sources don't have (`instrument_aliases`,
  `lei_blacklist`)
- Fuzzy title matching against the cached reference data (advisory only —
  ranks candidates, decides nothing)

## Public Service API

- `refresh_athex(db_path=None) -> int` — fetch + upsert ATHEX's current
  stock list. Safe to re-run.
- `refresh_gleif(db_path=None) -> GleifRefreshResult` — look up and link the
  LEI for any cached instrument that doesn't have one yet. Returns
  `.linked` and `.skipped_blacklisted`; the second exists because `linked`
  alone can't tell a run that found no LEI from one that suppressed a
  known-bad link (see `blacklist_lei()`), and those want different
  reactions. The CLI prints the skipped line only when it's non-zero.
- `lookup_by_isin(isin, db_path=None) -> Instrument | None`
- `lookup_by_lei(lei, db_path=None) -> Entity | None`
- `fuzzy_match_title(title, instrument_type=None, threshold=0.75, db_path=None) -> list[Instrument]`
- `fuzzy_match_title_scored(title, instrument_type=None, threshold=0.75, db_path=None) -> list[tuple[float, Instrument]]`
  — same matching, also returns each match's actual best ratio.
- `blacklist_lei(isin, lei, reason=None, db_path=None) -> None` — records that
  GLEIF's `isin`→`lei` link is known-wrong, and clears it if already
  written. `refresh_gleif()` then looks the ISIN up but won't re-apply
  that pair, so a hand-corrected linkage stays corrected instead of
  silently reappearing (it re-queries exactly the rows with `lei IS
  NULL`, i.e. the ones you just nulled). Keyed on the pair, not the ISIN
  — if GLEIF later returns a *different*, correct LEI for that ISIN, it
  links normally. Stored in its own `lei_blacklist` table, same
  can't-be-wiped-by-a-refresh reasoning as aliases. See the known GLEIF
  data-quality note at the bottom for the case that motivated it.
- `add_alias(isin, alias_text, source, confidence=None, db_path=None) -> None` — records a
  locally-learned alternate spelling of an instrument (e.g. a consuming
  project's own confirmed human/AI title-merge decision). Stored
  separately from `instruments`/`entities` (`refresh_athex()`/
  `refresh_gleif()` never touch it, so a refresh can't wipe a learned
  alias) and consulted by both `fuzzy_match_title*()` functions as an
  extra candidate string per instrument — an exact alias hit naturally
  scores ratio 1.0. The payoff: once a consuming project has reconciled
  two spelling variants as the same instrument, a *future* occurrence of
  that exact string resolves immediately, without needing to re-run
  fuzzy-matching/LLM review to rediscover the same match again.

## Installation

```bash
cd instrument_registry
uv venv
uv sync
```

## Database

By default, the local cache lives in the user's XDG data directory:

```text
${XDG_DATA_HOME:-~/.local/share}/instrument_registry/instrument_registry.db
```

Deliberately *outside* the package source tree, so that reinstalling,
cleaning or re-cloning the package can't destroy it. That matters mainly
for `instrument_aliases`: the ATHEX/GLEIF rows are re-fetchable at any
time by `refresh_athex()`/`refresh_gleif()`, but locally-learned aliases
are not recoverable from any external source. (Moved here 2026-07-24;
it previously sat at `src/instrument_registry/db/instrument_registry.db`,
inside the package.)

## CLI

```bash
python -m instrument_registry --refresh-athex
python -m instrument_registry --refresh-gleif
```

## Data sources

- **ATHEX**: `https://athens.euronext.com/sites/default/files/
  json_data_files/stocks_en.json` — a static JSON file (no auth). The
  site sits behind Cloudflare bot protection that blocks a plain/generic
  HTTP client; `collector/athex.py` uses a browser-header + HTTP/2 client
  to get through (confirmed live 2026-07-19), the same trick
  `pothen_eshes.http_client` uses for hellenicparliament.gr's Akamai
  protection.
- **GLEIF**: `https://api.gleif.org/api/v1/lei-records?filter[isin]=...`
  — GLEIF's free, no-auth, live search API, queried per-ISIN rather than
  bulk-downloading their full global "Golden Copy" file (Phase 1 only
  needs the ~150 entities behind ATHEX's own stock list, not GLEIF's
  entire global dataset).

## Testing

```bash
uv run pytest
```

Most tests run against small hand-seeded SQLite fixtures. A separate,
explicitly-run live test actually hits the real ATHEX/GLEIF sources to
confirm the collectors still work against them:

```bash
INSTRUMENT_REGISTRY_LIVE_TESTS=1 uv run pytest
```

## Notes

- Used in-process by consumers (e.g. `pothen_eshes`); not an HTTP
  service itself.
- `instrument_type` is hardcoded to `'stock'` for every ATHEX-sourced row
  — that source is stocks-only, not a guess.
- `cfi_code`/`currency` are NULL for every ATHEX-sourced row today —
  ATHEX's own stock-list JSON doesn't carry either field. Left as an open
  choice for whenever a source that does is added.
- `fuzzy_match_title()` matches against both the instrument's own
  name/other_names (ATHEX, English-only) *and*, when linked, the
  entity's legal_name/other_names (GLEIF, often Greek) — found and fixed
  same session (2026-07-19) after a live smoke test showed a Greek-script
  query for National Bank of Greece matching nothing, since ATHEX's own
  data never has a Greek name.
- **Known GLEIF data-quality issue (found live 2026-07-19, worked around
  locally):** GLEIF's own `filter[isin]=...` API currently returns
  Piraeus Bank's LEI (`213800OYHR1MPQ5VJL60`) for ISIN `GRK014011008`
  (ATHEX-listed as `MERMEREN KOMBINAT A.D. PRILEP`, an unrelated North
  Macedonian company) — confirmed this isn't a bug in `collector/
  gleif.py` (it correctly reflects records[0] from GLEIF's response, and
  a raw live query returns exactly one record, the wrong one). Piraeus
  Bank's own correct ATHEX entry (`GRS831003009`) is separately, and
  correctly, linked to the same LEI, so this is a genuine duplicate/wrong
  linkage on GLEIF's end, not this package's. Found by a consumer
  (`pothen_eshes`) auditing its own confirmed title merges against this
  package's data — several unrelated Greek bank-related titles were
  scoring falsely high against Mermeren Kombinat's (wrongly) Piraeus-Bank
  entity names. Corrected locally by nulling `instruments.lei` for that
  one ISIN; **note this will silently recur** on a future `refresh_gleif()`
  run (it only re-queries instruments with `lei IS NULL`, and GLEIF's
  live data hasn't changed) unless GLEIF's own registry is fixed
  upstream or this package grows a way to blacklist a known-bad
  ISIN→LEI link — not built, since one confirmed bad case wasn't enough
  to justify the added mechanism yet.
  **It did recur, exactly as predicted, on a `refresh_gleif()` run
  2026-07-24** — that run's entire output was "linked 1 instruments", and
  the 1 was this. A second occurrence was enough to justify the
  mechanism, so `blacklist_lei()` (above) now exists and this pair is
  recorded in it; a live `refresh_gleif()` on 2026-07-24 confirmed the
  link no longer comes back.
