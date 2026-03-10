#!/usr/bin/env python3
"""
migrate_summaries.py -- normalize chunk summaries to the canonical schema.

Usage:
    DB_PATH=data/alice.db python scripts/migrate_summaries.py
    DB_PATH=data/alice.db python scripts/migrate_summaries.py --dry-run

What it does:
  - Reads every summarized chunk from the DB
  - Parses the JSON (handles fences and embedded control chars)
  - Applies field-name aliases (e.g. emotional_tone -> mood)
  - Re-serializes as clean JSON (no fences, no control chars)
  - Writes back only rows that changed
  - Idempotent: running twice produces identical results

Alias map:
  emotional_tone -> mood
  main_topics    -> topics
  overview       -> summary
  narrative      -> summary
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running directly without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from wain import config
from wain.schema import (
    FIELD_ALIASES, REQUIRED_FIELDS,
    parse_summary_json, normalize_summary, validate_summary,
    validation_issues, serialize_summary,
)


def migrate(db_path: str, dry_run: bool = False) -> int:
    """
    Migrate all summaries. Returns exit code (0 = success, 1 = unfixable rows remain).
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, date_start, summary FROM chunks WHERE summary IS NOT NULL ORDER BY id"
    ).fetchall()

    total = len(rows)
    already_canonical = 0
    normalized = 0
    unparseable = []
    still_invalid = []

    print(f"Migrating {total} summaries in {db_path}{' [DRY RUN]' if dry_run else ''}...\n")

    for chunk_id, date, raw in rows:
        data = parse_summary_json(raw)

        if data is None:
            unparseable.append(chunk_id)
            print(f"  [chunk {chunk_id:3d} {date}] [FAIL]  UNPARSEABLE -- skipped")
            continue

        # Apply aliases
        normalized_data = normalize_summary(data)

        # Detect what changed
        changed_keys = {
            alias for alias in FIELD_ALIASES
            if alias in data and FIELD_ALIASES[alias] != alias
        }

        is_valid = validate_summary(normalized_data)
        issues = validation_issues(normalized_data)

        if not changed_keys:
            # Check if raw JSON needs cleaning (control chars or fences)
            clean = serialize_summary(normalized_data)
            if clean == raw.strip():
                already_canonical += 1
                continue  # Truly identical -- skip silently
            # Same fields but dirty serialization -- needs rewrite
            changed_keys = {"(re-serialized)"}

        clean_json = serialize_summary(normalized_data)

        status = "[ok]" if is_valid else "(!)"
        changes_str = ", ".join(f"{a}->{FIELD_ALIASES.get(a, a)}" for a in sorted(changed_keys))
        print(f"  [chunk {chunk_id:3d} {date}] {status}  {changes_str or 'cleaned'}")

        if not is_valid:
            still_invalid.append((chunk_id, issues))

        if not dry_run:
            conn.execute("UPDATE chunks SET summary = ? WHERE id = ?", (clean_json, chunk_id))
            normalized += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\n{'-' * 50}")
    print(f"  Total:            {total}")
    print(f"  Already canonical:{already_canonical:4d}")
    print(f"  Normalized:       {normalized:4d}")
    print(f"  Unparseable:      {len(unparseable):4d}" + (f"  (ids: {unparseable})" if unparseable else ""))
    print(f"  Still invalid:    {len(still_invalid):4d}" + (f"  (ids: {[r[0] for r in still_invalid]})" if still_invalid else ""))

    if still_invalid:
        print("\nRemaining issues:")
        for cid, issues in still_invalid:
            print(f"  chunk {cid}: {'; '.join(issues)}")

    if dry_run:
        print("\n[Dry run -- no changes written]")

    return 1 if (still_invalid or unparseable) else 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Show what would change without writing to DB")
    p.add_argument("--db", default=config.DB_PATH, help=f"Path to SQLite DB (default: {config.DB_PATH})")
    args = p.parse_args()

    sys.exit(migrate(args.db, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
