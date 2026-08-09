# BACKUP.md — the mutable asset and how it's backed up

Everything this repo produces or uses at runtime is gitignored (see
CLAUDE.md — "the cache DB is gitignored and must stay that way"). That's
the right call for the repo, but it means the durable copy has to be
accounted for somewhere. This file is that account.

## Inventory of mutable assets

There is exactly one: the local cache DB.

| Asset | Size | In git? | Regenerable? | Authoritative copy | Backed up by |
|---|---|---|---|---|---|
| `instrument_registry.db` | ~0.2 MB | No | **Partially** — `instruments`/`entities` yes, `instrument_aliases`/`lei_blacklist`/`title_isin_exclusions` no | This machine, or a `pothen_eshes` deployed volume if one is writing to a shared cache live | `backup.py` → `snapshots/` |

Default location: `${XDG_DATA_HOME:-~/.local/share}/instrument_registry/instrument_registry.db`
(see README.md → Database). Size is as of 2026-08-09 and will drift.

### Why the whole file, not just the irreplaceable tables

`instruments`/`entities` are rebuilt any time by `refresh_athex()`/
`refresh_gleif()` — in seconds, not the ~36 minutes a sibling repo
(`pothen_eshes`) needs to rebuild its own regenerable data. That's cheap
enough that splitting the DB into a "worth backing up" tier and a
"skip it, just re-fetch" tier isn't worth the complexity: the whole file
is under 200 KB, so it's backed up together, every time. The three
locally-learned tables (`instrument_aliases`, `lei_blacklist`,
`title_isin_exclusions`) are the ones that actually can't be
reconstructed from anywhere else — see CLAUDE.md's central invariant.

## Local backup

    uv run python -m instrument_registry --backup

Writes to `/home/dev-ubuntu/data/backup/instrument_registry` by default —
the same staging-area convention already used by `pothen_eshes` and
`euronext_athens_delisted_issuers` on this machine (`snapshots/` +
`MANIFEST.json` with sizes/SHA-256s), minus their separate `bulk/` tier,
which nothing here needs (see above). Meant to be copied onward to cloud
storage by hand; nothing here uploads anything.

- `snapshots/<utc-timestamp>/instrument_registry.db` — a full copy of the
  cache DB, versioned (kept: last 30 runs, `--keep` to change), so a bad
  write or a corrupted file is recoverable from an earlier run.
- Each snapshot's `MANIFEST.json` records size, SHA-256, the
  `instrument_registry` repo commit the backup was taken at, and a
  per-table row count — useful for spotting at a glance whether a backup
  actually captured the aliases/exclusions you expect, not just "some
  file of the right size."

The copy goes through `export_snapshot()` (`sqlite3.Connection.backup()` +
`serialize()`, not a raw file read) and is then
`PRAGMA integrity_check`-ed, never a plain `cp`/rsync — a file copy of a
database mid-write can capture a torn, unopenable snapshot.

## Warning: a shared deployed cache can be ahead of this one

If `pothen_eshes` (or any other consumer) is running against this same
cache from a deployed environment — e.g. a route calling `add_alias()`/
`exclude_title_match()` live — that copy is the **authoritative** one for
anything written that way, and this machine's local file can be behind
it. `export_snapshot()` is how a consumer downloads the deployed copy's
learned state back down (see README's Public Service API); nothing pulls
it automatically or on a schedule. A local backup here is only ever a
backup of what this machine's copy currently holds.

## Restoring

- Copy the snapshot's `instrument_registry.db` to the real `db_path`
  (default: the path in the snapshot's `MANIFEST.json` `"source"` field).
  Verify with `PRAGMA integrity_check` and against the `sha256` in
  `MANIFEST.json` first.
- `instruments`/`entities` can also just be rebuilt with
  `refresh_athex()`/`refresh_gleif()` instead of restoring — but
  `instrument_aliases`/`lei_blacklist`/`title_isin_exclusions` can't, so
  restoring the whole file (not re-running the refreshes) is what
  actually matters for those three.

## Not automated

Nothing uploads to cloud storage, and nothing runs `--backup` on a
schedule. Both are deliberate for now — the copy-to-cloud step is manual.
