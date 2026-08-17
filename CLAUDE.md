# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Shared reference-data service resolving "is this the same real company/stock?" against
actual ISO identifiers — ISIN (ISO 6166, instrument) and LEI (ISO 17442, issuing entity)
— instead of fuzzy-matching free-text titles against each other. Phase 1 is Greek stocks
only. Used **in-process** by consumers (`pothen_eshes`, which resolves this package from a
pinned git revision — see its own `pyproject.toml`); it is not an HTTP service. See
README.md for the domain reasoning and the full public API.

## Commands

```bash
uv sync                                    # install/reproduce env from uv.lock
uv run pytest                              # full suite, never touches the network
uv run pytest tests/test_service.py::test_refresh_gleif_does_not_relink_a_blacklisted_pair
INSTRUMENT_REGISTRY_LIVE_TESTS=1 uv run pytest    # also runs the real ATHEX/GLEIF tests
python -m instrument_registry --refresh-athex     # fetch + upsert ATHEX's stock list
python -m instrument_registry --refresh-gleif     # link LEIs for instruments missing one
python -m instrument_registry --backup            # back up the local cache DB (see BACKUP.md)
python -m instrument_registry --status            # read-only: what's in a cache, what's missing
python -m instrument_registry --merge-learned <snapshot.db>   # reconcile two copies' learned rows
```

No linter or formatter is configured — don't introduce one without asking.

## Architecture

Three layers, strictly one-directional (`collector/` → `service.py` → `db/`):

- **`collector/athex.py`, `collector/gleif.py`** — the *only* code that makes network
  calls. Each returns frozen dataclasses and knows nothing about SQLite.
- **`service.py`** — the entire public API, re-exported from `__init__.py`. The only
  module that touches both collectors and the DB.
- **`db/`** — `models.py` holds the schema as one `CREATE TABLE IF NOT EXISTS` script
  run on every `connect()`; `session.py` resolves the DB path.

`refresh_athex()` and `refresh_gleif()` are the only functions that hit the network.
Every lookup/match reads local SQLite exclusively — fetch once, cache, query fast.

### The central invariant: two kinds of table

The five tables split into two categories, and conflating them causes real data loss:

| Upstream-sourced, regenerable | Locally-learned, **irreplaceable** |
|---|---|
| `instruments`, `entities` | `instrument_aliases`, `lei_blacklist`, `title_isin_exclusions` |
| rebuilt any time by `refresh_*()` | recoverable from no external source |

`refresh_athex()`/`refresh_gleif()` must never write to the right-hand column — that's
what makes a refresh safe to re-run. The corollary bites when there is more than one
live copy of the cache (there is: this machine's, and `pothen_eshes`'s deployed volume):
the left-hand column never needs reconciling because a refresh rebuilds it, and the
right-hand column can only be reconciled by copying rows across. `--merge-learned`
(`merge.py`) is that operation — additive only, learned tables only, idempotent,
`created_at` preserved, and it **previews unless given `--apply`**. Reach for
`import_snapshot()` instead only when you mean to *replace* a copy wholesale, which
discards whatever the destination learned meanwhile.

**A shrunk learned table is not evidence of loss.** It is exactly as consistent with
"someone deliberately cleaned this up", and row counts cannot tell the two apart. This
bit for real: on 2026-08-16 four aliases were restored from an old backup on the
strength of a 99→95 count comparison, when they were in fact corrupted strings
(`ΑΕΡΟΛΙΜΕΝΑΣ`→`ΡΟΛΙΜΕΝΑΣ`, `ΧΑΛΚΟΥ`→`ΧΑΛ Υ` — an unbounded regex stripping `ΚΟ`/`ΑΕ`)
deleted on purpose a week earlier. Before restoring or merging any learned row, check
**reachability**, not counts: does the stored string appear in, or derive from, a real
title in the consumer's corpus? If nothing can reach it, no lookup can ever hit it and
its absence is a fix. A timestamp heuristic is not a substitute — it was tried and fired
on 0 of the 4 rows, because `add_alias()` restamps `created_at` with now().

When running that reachability check, **normalize whitespace before stripping
boilerplate** (`" ".join(title.split())` first). Consumer titles come from PDFs and a
line break can fall mid-phrase or mid-word, so a boilerplate phrase spanning a newline
survives a strip on one side and not the other — same title, two cores, and a good alias
looks unreachable. It fails in the destructive direction: it reports corruption that
isn't there and invites deleting a legitimate row. Same hazard for naive substring
tests (`LIKE '%ΡΟΛΙΜΕΝΑΣ%'` matches the intact `ΑΕΡΟΛΙΜΕΝΑΣ`). Assume a newline can be
anywhere in this corpus. `refresh_athex()`'s upsert also deliberately avoids
clobbering an existing row's `lei`/`cfi_code`/`currency` back to NULL, since a later step
fills those in. Before any destructive operation on the cache, back it up first —
`uv run python -m instrument_registry --backup` (see BACKUP.md).

### Things that are non-obvious from a single file

- **The default DB lives outside the repo**, at
  `${XDG_DATA_HOME:-~/.local/share}/instrument_registry/instrument_registry.db`,
  precisely so a clean or re-clone can't destroy the learned aliases. Every public
  function takes `db_path=` to override it; tests always pass a `tmp_path`.
- **`refresh_gleif()` only re-queries rows with `lei IS NULL`.** That means hand-nulling
  a bad link is exactly what makes the next refresh retry it — which is why
  `blacklist_lei()` exists rather than manual SQL. Blacklisting is keyed on the
  `(isin, lei)` *pair*, so an upstream fix returning a different, correct LEI still links.
- **GLEIF's own data can be wrong.** A confirmed live case (Piraeus Bank's LEI returned
  for Mermeren Kombinat's ISIN) is recorded in `lei_blacklist`. When a match looks absurd,
  verify against a raw live query before assuming this package has a bug.
- **`fuzzy_match_title_scored()` is the real implementation**; `fuzzy_match_title()` just
  drops the ratio/matched-candidate. It scores against the instrument's name, its
  `other_names`, its learned aliases, *and* the linked entity's GLEIF names. That last one
  matters: ATHEX's data is English-only, while consumers' titles are frequently
  Greek-script, and GLEIF is where the Greek legal name comes from. A caller recomputing a
  ratio against only `instrument.name` will get a wrong, too-low number. Since 2026-07-29 it
  also returns *which* candidate string (name/other_names/alias/entity name) actually won,
  not just the ratio — plain string similarity can't distinguish a genuine name match from
  two unrelated companies sharing generic corporate boilerplate (e.g. "ΣΥΜΜΕΤΟΧΩΝ Α.Ε.");
  showing the reviewer which string matched makes that visible. `title_isin_exclusions`
  (via `exclude_title_match()`) is the fix for a confirmed case of exactly that: it drops
  an excluded `(title, isin)` pair from the candidate list entirely, before ranking, so a
  wrong candidate can't keep outranking the real one no matter how many times matching
  re-runs.
- Matching is **advisory** — it ranks candidates and decides nothing.
- **A schema migration that adds a column doesn't backfill it.** `db/models.py`'s
  `_add_column()` can only run `ALTER TABLE ADD COLUMN` — existing rows get that column
  as NULL until something re-fetches real values into it. This bit `instruments.symbol`
  for real: the migration shipped, but nobody re-ran `refresh_athex()` against the live
  cache (local or `pothen_eshes`'s deployed one) until it was noticed weeks later during
  a backup/restore drill. `_add_column()` now prints a one-time stderr notice the moment
  it actually runs, naming the refresh that backfills it — but that only helps if
  someone's watching stderr on every environment that has this cache. Treat "which
  refresh backfills this column, and has it been re-run everywhere the cache is
  deployed" as a required step of writing the migration, not an afterthought.
- **Shipping a refresh isn't the same as running it.** The same gap bit
  `refresh_athex_etfs()` in a shape the migration notice above can't catch: it added
  no column, so nothing fired, and the cache simply held zero ETFs from the day it
  merged (2026-08-09) until someone ran it (2026-08-16) — which also surfaced that it
  had never populated `symbol` at all. `--status` (`status.py`) is the general check:
  read-only, safe against a deployed cache, and it answers "has each refresh ever run
  here, is what it owns complete, and has a learned table shrunk since the last
  backup" from the data alone. **Run it on every cache after merging anything that
  changes what a refresh writes** — and add the new refresh to `status.py`'s
  `REFRESHES` in the same commit, or its rows are invisible to the very check meant
  to notice invisible rows (there's a test guarding exactly that).

## Working in this repo

- **External sources get verified live, not just mocked.** Both collectors have real
  network tests gated behind `INSTRUMENT_REGISTRY_LIVE_TESTS`; the mocked suite alone is
  not evidence a source still works. ATHEX sits behind Cloudflare and needs the
  browser-header + HTTP/2 client in `collector/athex.py` — a plain HTTP client gets a 403.
- **Update README.md in the same commit as the code it describes**, not as a follow-up.
  The README carries the field-tested gotchas (data-quality issues, why a source is
  queried the way it is); it's the reason this package's history is legible.
- Dependencies via `uv` — `uv add <package>` updates `pyproject.toml` + `uv.lock`, both
  committed.
- The cache DB is gitignored and must stay that way. `backup.py` (`--backup`) is how it
  gets backed up locally — see BACKUP.md.

## Git/PR workflow

Standing authorization — no need to ask before each push or merge:

- Push commits directly (feature branches, not `main`) without asking first.
- Open a PR per logical change (keep doing this — it's the review/revert boundary), then
  **merge it yourself** once `uv run pytest` is green (run
  `INSTRUMENT_REGISTRY_LIVE_TESTS=1 uv run pytest` too when the change touches a
  collector). Don't wait for manual approval on the PR itself.
- Squash-merge (`gh pr merge --squash`), and delete the branch after
  (`--delete-branch`, or pass both together).
- This repo has no CI and no branch protection — the test run above *is* the merge
  gate, so don't skip it. (The old reason for the gap, that branch protection wasn't
  available for a private repo on the free tier, expired when this repo went **public
  on 2026-08-16**; it could be turned on now. Until it is, nothing but that test run
  stands between a push and `main`.)
- **This repo is public — anything committed here is world-readable, permanently.**
  That's deliberate: `pothen_eshes` pins this package by git revision, and while it was
  private, a consumer running in a sandbox whose credentials were scoped to its own
  repository couldn't install at all (that project's issue #21). Nothing here is
  sensitive — public-source reference data plus the code that fetches it — but the bar
  for a new file is now "would I publish this", not "is this useful". The cache DB in
  particular stays gitignored, as it always has been.
- Still ask first for anything actually destructive or hard to reverse — force-push,
  history rewrites, deleting `main`, or anything touching the cache DB per the backup
  rule above. This authorization covers routine push/merge only.
