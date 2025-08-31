#!/usr/bin/env python3
"""
merge_sessions.py — Merge all log rows from one session into another in a Cycle Analyst SQLite DB.

Usage:
  python merge_sessions.py --db ride_data.db \
      --source 2025-08-02_13-17-40 \
      --target 2025-08-02_12-06-15

Options:
  --dry-run       : Show what would happen without changing the DB.
  --no-vacuum     : Skip VACUUM at the end.
  --keep-source   : Do not delete the source session row from `sessions` table (if that table exists).

What it does:
  1) Creates a timestamped backup of the DB in the same directory.
  2) Verifies the `logs` table exists and has a `session` column.
  3) Reassigns all `logs.session` values from <source> to <target> in a single transaction.
  4) If a `sessions` table exists, deletes the <source> row (unless --keep-source).
  5) VACUUMs the DB (unless --no-vacuum).

Notes:
  - Row IDs in `logs` are left untouched.
  - Chronological order is preserved by your timestamps — no reordering is needed.
  - If the target session doesn't exist in `logs`, that's fine; rows will still be moved.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import datetime

def table_exists(cur, name):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (name,))
    return cur.fetchone() is not None

def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table});")
    return any(row[1] == column for row in cur.fetchall())

def make_backup(db_path):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(db_path)
    backup_path = f"{base}.backup_{ts}{ext or '.db'}"
    shutil.copy2(db_path, backup_path)
    return backup_path

def count_rows(cur, table, where_clause="", params=()):
    q = f"SELECT COUNT(*) FROM {table}"
    if where_clause:
        q += f" WHERE {where_clause}"
    cur.execute(q, params)
    (n,) = cur.fetchone()
    return n

def main():
    ap = argparse.ArgumentParser(description="Merge logs from one session into another in a Cycle Analyst DB.")
    ap.add_argument("--db", required=True, help="Path to SQLite DB file (e.g., ride_data.db)")
    ap.add_argument("--source", required=True, help="Session ID to move from (e.g., 2025-08-02_13-17-40)")
    ap.add_argument("--target", required=True, help="Session ID to move into (e.g., 2025-08-02_12-06-15)")
    ap.add_argument("--dry-run", action="store_true", help="Don't modify the DB; just show what would happen.")
    ap.add_argument("--no-vacuum", action="store_true", help="Skip VACUUM at the end.")
    ap.add_argument("--keep-source", action="store_true", help="Keep the source row in `sessions` table (if exists).")
    args = ap.parse_args()

    db_path = args.db
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    # Backup first
    backup_path = make_backup(db_path)
    print(f"Backup created: {backup_path}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Basic schema checks
    if not table_exists(cur, "logs"):
        print("ERROR: Table `logs` not found. Aborting.", file=sys.stderr)
        sys.exit(2)
    if not column_exists(cur, "logs", "session"):
        print("ERROR: Column `session` not found in `logs`. Aborting.", file=sys.stderr)
        sys.exit(3)

    has_sessions_table = table_exists(cur, "sessions")
    if has_sessions_table and not column_exists(cur, "sessions", "session"):
        print("WARNING: `sessions` table exists but has no `session` column; it will be ignored.")
        has_sessions_table = False

    source = args.source
    target = args.target

    n_src = count_rows(cur, "logs", "session = ?", (source,))
    n_tgt = count_rows(cur, "logs", "session = ?", (target,))
    n_total = count_rows(cur, "logs")

    print(f"Rows in logs: total={n_total}, source={n_src}, target={n_tgt}")
    if n_src == 0:
        print("Nothing to merge: no rows found for --source. Exiting.")
        sys.exit(0)

    # If dry-run, stop here
    if args.dry_run:
        print("[DRY RUN] Would execute: UPDATE logs SET session = ? WHERE session = ?;")
        if has_sessions_table and not args.keep_source:
            print("[DRY RUN] Would execute: DELETE FROM sessions WHERE session = ?;")
        print("No changes applied.")
        sys.exit(0)

    try:
        cur.execute("BEGIN;")
        # Move rows
        cur.execute("UPDATE logs SET session = ? WHERE session = ?;", (target, source))
        moved = cur.rowcount

        # Optionally delete source session row in `sessions`
        deleted_sessions = 0
        if has_sessions_table and not args.keep_source:
            cur.execute("DELETE FROM sessions WHERE session = ?;", (source,))
            deleted_sessions = cur.rowcount

        con.commit()
        print(f"Merged {moved} row(s) from '{source}' into '{target}'.")
        if has_sessions_table:
            print(f"Deleted {deleted_sessions} row(s) from `sessions` (source='{source}').")

    except Exception as e:
        con.rollback()
        print("ERROR: Transaction rolled back due to exception:", e, file=sys.stderr)
        sys.exit(4)
    finally:
        cur.close()
        con.close()

    # Optional VACUUM to tidy up
    if not args.no_vacuum:
        try:
            con2 = sqlite3.connect(db_path)
            con2.execute("VACUUM;")
            con2.close()
            print("VACUUM completed.")
        except Exception as e:
            print("WARNING: VACUUM failed:", e)

    print("Done. You can now remove the old session from any UI list if it still appears cached.")

if __name__ == "__main__":
    main()
