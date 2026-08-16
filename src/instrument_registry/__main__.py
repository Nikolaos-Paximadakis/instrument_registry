import sys

from instrument_registry.service import refresh_athex, refresh_athex_etfs, refresh_gleif


def main() -> None:
    if "--refresh-athex" in sys.argv:
        count = refresh_athex()
        print(f"instrument_registry: upserted {count} ATHEX-sourced instruments.")
    elif "--refresh-athex-etfs" in sys.argv:
        count = refresh_athex_etfs()
        print(f"instrument_registry: upserted {count} ATHEX-sourced ETFs.")
    elif "--refresh-gleif" in sys.argv:
        result = refresh_gleif()
        print(f"instrument_registry: linked {result.linked} instruments to a GLEIF entity (LEI).")
        if result.skipped_blacklisted:
            print(
                f"instrument_registry: skipped {result.skipped_blacklisted} known-bad "
                "ISIN->LEI link(s); see the lei_blacklist table."
            )
    elif "--backup" in sys.argv:
        from instrument_registry.backup import main as backup_main
        raise SystemExit(backup_main([a for a in sys.argv[1:] if a != "--backup"]))
    elif "--status" in sys.argv:
        from instrument_registry.status import main as status_main
        raise SystemExit(status_main([a for a in sys.argv[1:] if a != "--status"]))
    else:
        raise SystemExit(
            "Usage: python -m instrument_registry "
            "--refresh-athex | --refresh-athex-etfs | --refresh-gleif | "
            "--backup | --status"
        )


if __name__ == "__main__":
    main()
