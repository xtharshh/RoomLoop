"""CLI entry point: seed or reset the local RoomLoop database.

    python -m roomloop.seed --db roomloop.db --reset
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from .db import Database
from .demo import reset_to_demo_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed RoomLoop with demo data")
    parser.add_argument("--db", default="roomloop.db", help="SQLite path (default: roomloop.db)")
    parser.add_argument("--reset", action="store_true", help="Delete existing bookings first")
    parser.add_argument("--today", help="Anchor date for relative demo data (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    anchor = date.fromisoformat(args.today) if args.today else None
    db = Database(args.db)
    if not args.reset:
        existing = db.bookings_between(0, 2**31 - 1)
        if existing:
            print("Database already has bookings; pass --reset to replace them.", file=sys.stderr)
            return 1

    summary = reset_to_demo_state(db, anchor)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
