from __future__ import annotations

import argparse
import csv
from pathlib import Path

from kefu_identity_service.store import IdentityStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import sanitized identity mappings into SQLite")
    parser.add_argument("csv_file", type=Path)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/identity.db"),
        help="identity SQLite path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = IdentityStore(args.database)
    imported = 0
    try:
        with args.csv_file.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                store.upsert_identity(dict(row))
                imported += 1
    finally:
        store.close()
    print(f"Imported {imported} identity mapping(s) into {args.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
