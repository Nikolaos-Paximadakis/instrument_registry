import sys

from instrument_registry.service import refresh_athex, refresh_gleif


def main() -> None:
    if "--refresh-athex" in sys.argv:
        count = refresh_athex()
        print(f"instrument_registry: upserted {count} ATHEX-sourced instruments.")
    elif "--refresh-gleif" in sys.argv:
        count = refresh_gleif()
        print(f"instrument_registry: linked {count} instruments to a GLEIF entity (LEI).")
    else:
        raise SystemExit("Usage: python -m instrument_registry --refresh-athex | --refresh-gleif")


if __name__ == "__main__":
    main()
