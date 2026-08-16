"""Report what a given cache DB actually contains, and what's missing
from it.

Run it as:

    uv run python -m instrument_registry --status

This exists because of a failure mode this package has now hit twice, in
two different shapes, and that no test can catch: **code shipping is not
the same as the cache being updated.** A cache is a long-lived mutable
asset that lives outside the repo (and, for a consumer like
`pothen_eshes`, outside this machine), so a change that only takes
effect on the next refresh sits dormant on every deployment where nobody
re-ran it:

- `instruments.symbol` was added by a migration, and stayed NULL on the
  real cache for weeks because nothing re-ran `refresh_athex()`.
  `db/models.py`'s `_add_column()` now prints a one-time notice for
  exactly that — but only at the moment the `ALTER` runs, only on
  stderr, and only for a *column*.
- `refresh_athex_etfs()` shipped and was simply never run anywhere, so
  the cache held zero ETFs. No column was added, so the notice above
  couldn't have fired; nothing was wrong with the code at all.

The generalisation is a question you can ask a cache at any time, on any
machine, without knowing its history: *for each refresh this package
ships, has it ever run here, and is what it owns still complete?* That
is what this reports. It's a read-only diagnostic — it never fetches and
never writes, so it's safe to point at a production cache.

Exit code is 1 when something needs a human, 0 when the cache is
complete, so it works as a cron/deploy check and not just as something
to read.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from instrument_registry.backup import DEFAULT_ROOT as DEFAULT_BACKUP_ROOT
from instrument_registry.db.session import DEFAULT_DB_PATH

#: Every refresh that owns rows in `instruments`, keyed by the
#: `instrument_type` it writes. That type is what makes "has this refresh
#: ever run against this cache?" answerable from the data alone — there's
#: no run-history table, and deliberately so: a cache restored from a
#: backup, or copied off a deployment, would carry a stale history with
#: it, whereas the rows themselves are the truth.
#:
#: `backfills` lists the columns that refresh is responsible for filling.
#: A row of its own type with one of them NULL means the row predates a
#: change to that refresh and re-running it is what fixes the row — which
#: is precisely the `symbol` bug, made visible instead of silent.
REFRESHES = (
    {
        "command": "--refresh-athex",
        "instrument_type": "stock",
        "backfills": ("symbol",),
    },
    {
        "command": "--refresh-athex-etfs",
        "instrument_type": "etf",
        "backfills": ("symbol",),
    },
)

#: Locally-learned and irreplaceable — see CLAUDE.md's "two kinds of
#: table". Reported separately from the upstream ones because a drop in
#: these is a data-loss incident, while a drop in `instruments` is just
#: ATHEX delisting something.
LEARNED_TABLES = ("instrument_aliases", "lei_blacklist", "title_isin_exclusions")

UPSTREAM_TABLES = ("instruments", "entities")


def _count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _latest_backup(backup_root: Path) -> dict | None:
    """The row counts recorded by the most recent `--backup`, if there is
    one. `backup.py` already writes a per-table `row_counts` into each
    snapshot's MANIFEST.json, so comparing against it costs one file read
    and needs no new bookkeeping."""
    manifest_path = backup_root / "snapshots" / "latest" / "MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    snapshot = manifest.get("snapshot", {}).get("instrument_registry.db")
    if not snapshot:
        return None
    return {
        "created_utc": manifest.get("created_utc"),
        "row_counts": snapshot.get("row_counts", {}),
    }


def status(
    db_path: str | Path | None = None,
    *,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
) -> dict:
    """Inspects `db_path` read-only and returns a report dict. Never
    creates the DB: unlike every other entry point here, this one opens
    the file in SQLite read-only mode rather than going through
    `connect()`, because `connect()` runs `create_schema()` and would
    turn "you pointed --status at the wrong path" into "a new empty cache
    now exists there, reporting zero of everything"."""
    path = Path(db_path) if db_path is not None else Path(DEFAULT_DB_PATH)
    report: dict = {"db_path": str(path), "exists": path.exists(), "problems": []}

    if not report["exists"]:
        report["problems"].append(f"no cache DB at {path}")
        return report

    report["bytes"] = path.stat().st_size
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        report["tables"] = {
            table: _count(connection, table)
            for table in UPSTREAM_TABLES + LEARNED_TABLES
        }

        report["refreshes"] = []
        for refresh in REFRESHES:
            row = connection.execute(
                "SELECT COUNT(*) AS rows, MAX(updated_at) AS last_updated "
                "FROM instruments WHERE instrument_type = ?",
                (refresh["instrument_type"],),
            ).fetchone()
            missing = {
                column: connection.execute(
                    f"SELECT COUNT(*) FROM instruments "
                    f"WHERE instrument_type = ? AND {column} IS NULL",
                    (refresh["instrument_type"],),
                ).fetchone()[0]
                for column in refresh["backfills"]
            }
            entry = {
                "command": refresh["command"],
                "instrument_type": refresh["instrument_type"],
                "rows": row["rows"],
                "last_updated": row["last_updated"],
                "missing": {c: n for c, n in missing.items() if n},
            }
            if not entry["rows"]:
                entry["state"] = "never run here"
                report["problems"].append(
                    f"{refresh['command']} has never been run against this cache "
                    f"(no '{refresh['instrument_type']}' rows)"
                )
            elif entry["missing"]:
                entry["state"] = "needs re-run"
                for column, count in entry["missing"].items():
                    report["problems"].append(
                        f"{count} '{refresh['instrument_type']}' row(s) have a NULL "
                        f"{column}; re-run {refresh['command']} to backfill"
                    )
            else:
                entry["state"] = "ok"
            report["refreshes"].append(entry)

        # An instrument_type present in the data but not in REFRESHES means
        # this module is out of date with the package — worth saying out
        # loud, since the whole point is to be exhaustive about coverage.
        known = {r["instrument_type"] for r in REFRESHES}
        report["unrecognised_types"] = sorted(
            str(r[0]) for r in connection.execute(
                "SELECT DISTINCT instrument_type FROM instruments")
            if r[0] not in known
        )
        for unknown in report["unrecognised_types"]:
            report["problems"].append(
                f"instruments hold type '{unknown}', which no known refresh owns "
                "— status.py's REFRESHES is out of date"
            )

        # A NULL lei is NOT a problem: plenty of smaller Greek issuers have
        # no registered LEI at all, and refresh_gleif() re-queries those
        # every run by design. Reported as information, never as an error.
        report["gleif"] = {
            "linked": connection.execute(
                "SELECT COUNT(*) FROM instruments WHERE lei IS NOT NULL").fetchone()[0],
            "unlinked": connection.execute(
                "SELECT COUNT(*) FROM instruments WHERE lei IS NULL").fetchone()[0],
            "blacklisted_pairs": _count(connection, "lei_blacklist"),
        }
    finally:
        connection.close()

    backup = _latest_backup(backup_root)
    report["backup"] = backup
    if backup is None:
        report["problems"].append(
            f"no backup found under {backup_root} — run "
            "`python -m instrument_registry --backup`"
        )
    else:
        # Only the learned tables. `instruments` shrinking is normal (ATHEX
        # delists things); a learned table shrinking is not recoverable from
        # any external source, so it's the one comparison worth alarming on.
        # This check exists because four learned aliases really did go
        # missing between one backup and the next, and nothing noticed.
        shrunk = {}
        for table in LEARNED_TABLES:
            then = backup["row_counts"].get(table)
            now = report["tables"][table]
            if then is not None and now < then:
                shrunk[table] = {"backup": then, "now": now}
                report["problems"].append(
                    f"{table} has {now} row(s), down from {then} at the last backup "
                    f"({backup['created_utc']}) — learned data is not recoverable "
                    "from any external source"
                )
        report["backup"]["shrunk"] = shrunk

    return report


def _format(report: dict) -> str:
    lines = [f"cache: {report['db_path']}"]
    if not report["exists"]:
        lines.append("  MISSING — no database at that path")
        return "\n".join(lines)

    lines.append(f"  {report['bytes'] / 1e6:.3f} MB")
    lines.append("")
    lines.append("refreshes:")
    for entry in report["refreshes"]:
        detail = f"{entry['rows']} row(s)"
        if entry["last_updated"]:
            detail += f", last {entry['last_updated']}"
        if entry["missing"]:
            detail += "; NULL " + ", ".join(
                f"{c}x{n}" for c, n in entry["missing"].items())
        marker = "ok " if entry["state"] == "ok" else "!! "
        lines.append(f"  {marker}{entry['command']:<22} {entry['state']:<14} {detail}")

    gleif = report["gleif"]
    lines.append(
        f"  -- --refresh-gleif       {gleif['linked']} linked, "
        f"{gleif['unlinked']} unlinked (a NULL lei is normal), "
        f"{gleif['blacklisted_pairs']} blacklisted pair(s)"
    )

    lines.append("")
    lines.append("tables:")
    for table in UPSTREAM_TABLES:
        lines.append(f"  {table:<24} {report['tables'][table]}")
    for table in LEARNED_TABLES:
        lines.append(f"  {table:<24} {report['tables'][table]}  (learned, irreplaceable)")

    lines.append("")
    backup = report.get("backup")
    if backup is None:
        lines.append("backup: none found")
    else:
        lines.append(f"backup: latest {backup['created_utc']}")
        for table, counts in backup.get("shrunk", {}).items():
            lines.append(f"  !! {table}: {counts['backup']} -> {counts['now']}")

    if report["problems"]:
        lines.append("")
        lines.append(f"{len(report['problems'])} problem(s):")
        lines.extend(f"  - {problem}" for problem in report["problems"])
    else:
        lines.append("")
        lines.append("no problems.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report what a cache DB contains and what's missing from it. "
                    "Read-only: never fetches, never writes.")
    parser.add_argument("--db-path", type=Path, default=None,
                        help=f"cache to inspect (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT,
                        help=f"backup staging area to compare against "
                             f"(default: {DEFAULT_BACKUP_ROOT})")
    parser.add_argument("--json", action="store_true",
                        help="emit the raw report as JSON instead of text")
    args = parser.parse_args(argv)

    report = status(db_path=args.db_path, backup_root=args.backup_root)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(_format(report))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
