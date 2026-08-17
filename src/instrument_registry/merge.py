"""Merge locally-learned rows from one cache into another.

    python -m instrument_registry --merge-learned <snapshot.db>

Exists because there is more than one live copy of this cache — this
machine's, and the one on `pothen_eshes`'s deployed volume — and they
drift. `refresh_athex()`/`refresh_gleif()` can rebuild `instruments` and
`entities` on either copy at any time, so those never need reconciling.
The other three tables (`instrument_aliases`, `lei_blacklist`,
`title_isin_exclusions`) are recoverable from no external source, so a
row that exists on only one copy exists once in the world. Until now
nothing moved such a row between copies: `export_snapshot()`/
`import_snapshot()` deal in whole databases, which means using them to
carry four aliases across also overwrites everything else, including
whatever the destination learned that the source hasn't got.

So the semantics here are deliberately narrow:

- **Additive only.** Rows are inserted, never updated and never deleted.
  A merge cannot lose data on either side, which is what makes it safe
  to run against a live deployed cache. Restoring a copy wholesale is a
  different operation and remains `import_snapshot()`'s job.
- **Learned tables only.** `instruments`/`entities` are untouched even
  when the source has rows the destination lacks — those come from a
  refresh, and quietly seeding them from a stale snapshot would plant
  upstream data that no longer matches upstream.
- **Idempotent**, via each table's natural key (`(isin, alias_text)`,
  `(isin, lei)`, `(isin, title_text)`). Re-running merges nothing new.
- **Provenance preserved.** `source`, `confidence`, `reason` and
  crucially `created_at` are carried across verbatim rather than
  restamped with now(). A restore that rewrites `created_at` destroys
  the one piece of evidence that dates a loss — which is exactly what
  made the 2026-08-16 alias loss so hard to place.

Rows whose ISIN the destination doesn't have are skipped, not inserted:
both `instrument_aliases` and `title_isin_exclusions` reference
`instruments(isin)`, and an alias for an instrument that isn't there is
unreachable by every lookup in the package. That's a signal the
destination needs a refresh first, so it's reported rather than silently
dropped — re-run the merge afterwards to pick them up.

## The deletion trap

An additive merge cannot carry a deletion, and that is not a gap to be
filled — it is a limit worth being loud about. A row present in the
source and absent from the destination has two completely different
explanations that look identical from here:

1. the destination never received it, or
2. the destination *deliberately deleted* it.

Merging is right for (1) and actively destructive for (2): it reinstates
whatever was cleaned up, and on a deployed copy it does so in
production. `--dry-run` shows which rows would move but cannot say
whether they deserve to.

This is not hypothetical. On 2026-08-16 four `instrument_aliases` were
"restored" into the local cache from an old backup after a count
comparison read 99→95 as silent loss. They were in fact corrupted
strings — an unbounded boilerplate regex had stripped `ΚΟ`/`ΑΕ` without
word boundaries, turning ΑΕΡΟΛΙΜΕΝΑΣ into `ΡΟΛΙΜΕΝΑΣ` and ΧΑΛΚΟΥ into
`ΧΑΛ Υ` — and had been deliberately deleted from both copies a week
earlier. A merge in that direction would have put all four back into
production.

**A timestamp heuristic was tried for this and deliberately rejected.**
The idea was to flag any source row older than the destination's newest
row in the same table, on the reasoning that the destination has plainly
been written since and still doesn't have it. Tested against the real
incident it fires on **0 of the 4 rows it exists for**: the volume's
newest alias is 2026-07-19T11:19:53 and the four carry
2026-08-16T07:28:50, because `add_alias()` stamps `now()` rather than
preserving a row's origin. The act that creates the hazard is the same
one that erases the evidence, so the check would have sat silent while
looking like protection — worse than no check.

So this module does not infer intent from absence. It **reports by
default and writes only under `--apply`**, which puts a human in front
of the actual row list before anything moves. That is protection
proportional to what can honestly be known here.

What settles such a case is *reachability* — whether the stored string
appears in, or derives from, any real title in the consumer's corpus. A
string reachable from nothing can never be matched, so its absence is a
fix rather than a loss. That corpus lives in the consumer, not here, so
this package can only raise the question, never answer it.

**Reachability has a precondition, and getting it wrong is destructive.**
Normalize whitespace *before* stripping boilerplate, never after —
`" ".join(title.split())` first. Consumer titles come out of PDFs and a
line break can fall anywhere, including inside a word or in the middle
of a boilerplate phrase. `MIG ΑΝΩΝΥΜΟΣ\nΕΤΑΙΡΕΙΑ\nΣΥΜΜΕΤΟΧΩΝ (KO)` is
real: a stripper run over the raw string never sees `ΑΝΩΝΥΜΟΣ ΕΤΑΙΡΕΙΑ`
as a phrase and leaves it in, while the harvest path works from
already-normalized text and removes it. Same title, two different cores,
and a perfectly good alias is reported unreachable. Normalizing first,
the same corpus gives 183 aliases and 0 orphans.

Note which way that fails: the check cries corruption where there is
none, and a reader who trusts it deletes a legitimate row. A check whose
false positives are destructive has to state its preconditions rather
than imply them.

The same trap catches naive substring tests, and it has a specific fix.
`LIKE '%ΡΟΛΙΜΕΝΑΣ%'` matches the intact `ΑΕΡΟΛΙΜΕΝΑΣ` it was meant to
distinguish from the corrupted form; `LIKE '%ΡΙΝΘΟΥ%'` returns three
perfectly good `ΚΟΡΙΝΘΟΥ` rows. **The corruption's signature is the
space** — the eaten `ΚΟ`/`ΑΕ` leaves one where the intact word has a
letter — so anchoring on it restores the distinction: `'% ΡΟΛΙΜΕΝΑΣ%'`
and `'% ΡΙΝΘΟΥ%'` both return 0 against the same 183-row cache.

Better still, **don't pattern-match for corruption when you can test for
it**. Equality against the known-bad strings returns 0, and a
reachability sweep over all 183 rows returns 0 orphans; both are immune
to this whole class of error, which no `LIKE` ever is. Use a pattern to
explore, never to conclude.

The durable fix is to make deletion an **additive fact** — a tombstone
written by `remove_alias()`/`unblacklist_lei()`/`remove_title_exclusion()`
that merges in both directions like any other row, so a cleanup survives
a merge from a stale copy and intent never has to be guessed. That is a
schema change and is not implemented here yet.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from instrument_registry.db.session import DEFAULT_DB_PATH, connect

# table -> (natural key columns, all columns carried across)
LEARNED_TABLES = {
    "instrument_aliases": (
        ("isin", "alias_text"),
        ("isin", "alias_text", "source", "confidence", "created_at"),
    ),
    "lei_blacklist": (
        ("isin", "lei"),
        ("isin", "lei", "reason", "created_at"),
    ),
    "title_isin_exclusions": (
        ("isin", "title_text"),
        ("isin", "title_text", "reason", "created_at"),
    ),
}

# Only these reference instruments(isin); lei_blacklist deliberately does
# not, so a blacklisted pair survives its instrument disappearing.
NEEDS_INSTRUMENT = ("instrument_aliases", "title_isin_exclusions")


def merge_learned(
    source: Path | str,
    db_path: Path | str | None = None,
    *,
    apply: bool = False,
) -> dict:
    """Merges the three learned tables from `source` into the cache at
    `db_path`. Reports without writing unless `apply` — see "The deletion
    trap" above for why that's the default."""
    source = Path(source).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"no snapshot at {source}")

    # Read-only URI: the source is somebody's backup or a copy pulled off
    # a deployed volume, and a merge has no business modifying it — not
    # even to the extent connect() would, by running create_schema().
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        present = {
            row[0] for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing = [t for t in LEARNED_TABLES if t not in present]
        if missing:
            raise ValueError(
                f"{source} is missing {', '.join(missing)} — not an "
                "instrument_registry cache?")

        dest = connect(db_path)
        try:
            known_isins = {
                row[0] for row in dest.execute("SELECT isin FROM instruments")
            }
            report: dict = {
                "source": str(source),
                "destination": str(Path(db_path) if db_path else DEFAULT_DB_PATH),
                "applied": apply,
                "tables": {},
            }

            for table, (key_columns, columns) in LEARNED_TABLES.items():
                existing = {
                    tuple(row) for row in dest.execute(
                        f"SELECT {', '.join(key_columns)} FROM {table}")
                }
                added, skipped = [], []
                for row in src.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                    key = tuple(row[c] for c in key_columns)
                    if key in existing:
                        continue
                    if table in NEEDS_INSTRUMENT and row["isin"] not in known_isins:
                        skipped.append(dict(row))
                        continue
                    if apply:
                        dest.execute(
                            f"INSERT INTO {table} ({', '.join(columns)}) "
                            f"VALUES ({', '.join('?' for _ in columns)}) "
                            f"ON CONFLICT DO NOTHING",
                            tuple(row[c] for c in columns),
                        )
                    added.append(dict(row))

                report["tables"][table] = {
                    "added": len(added),
                    "already_present": len(existing),
                    "skipped_unknown_isin": len(skipped),
                    "added_rows": added,
                    "skipped_rows": skipped,
                }

            if apply:
                dest.commit()
            return report
        finally:
            dest.close()
    finally:
        src.close()


def _render(report: dict) -> str:
    lines = [
        f"source:      {report['source']}",
        f"destination: {report['destination']}",
    ]
    if not report["applied"]:
        lines.append("(preview — nothing was written; pass --apply to commit)")
    lines.append("")

    total_added = total_skipped = 0
    for table, result in report["tables"].items():
        total_added += result["added"]
        total_skipped += result["skipped_unknown_isin"]
        note = ""
        if result["skipped_unknown_isin"]:
            note = f", {result['skipped_unknown_isin']} skipped (ISIN not in destination)"
        lines.append(
            f"  {table:24} +{result['added']} "
            f"({result['already_present']} already present{note})")
        for row in result["added_rows"]:
            label = row.get("alias_text") or row.get("title_text") or row.get("lei")
            lines.append(f"      + {row['isin']}  {label}")
        for row in result["skipped_rows"]:
            label = row.get("alias_text") or row.get("title_text") or row.get("lei")
            lines.append(f"      ! {row['isin']}  {label}")

    lines.append("")
    verb = "merged" if report["applied"] else "would merge"
    lines.append(f"{verb} {total_added} learned row(s).")
    if total_skipped:
        lines.append(
            f"{total_skipped} row(s) skipped because the destination has no such "
            "instrument — run the relevant refresh there, then merge again.")
    if total_added and not report["applied"]:
        lines.append(
            "\nCheck these rows before applying. A row missing from the destination "
            "may be one it never received — or one it deliberately deleted, which an "
            "additive merge would reinstate. Nothing here can tell those apart; see "
            "\"The deletion trap\" in merge.py.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge locally-learned rows (aliases, LEI blacklist, title "
                    "exclusions) from a snapshot into a cache. Additive only: "
                    "never updates or deletes, and never touches instruments/"
                    "entities. Previews by default — an additive merge cannot "
                    "carry a deletion, so read the rows before applying.")
    parser.add_argument("source", type=Path,
                        help="snapshot DB to merge FROM (read-only)")
    parser.add_argument("--db-path", type=Path, default=None,
                        help=f"cache to merge INTO (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="actually write. Without this, nothing is committed.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        report = merge_learned(args.source, args.db_path, apply=args.apply)
    except (FileNotFoundError, ValueError) as exc:
        print(f"instrument_registry.merge: {exc}")
        return 2

    print(json.dumps(report, indent=2, ensure_ascii=False) if args.as_json
          else _render(report))
    return 0
