#!/usr/bin/env python3
"""Data-preserving rollback artifact for the block_reason guard triggers.

Drops ONLY the three guard triggers from kanban boards so pre-candidate
writers (956) can operate again. Never drops a table, column, or row:
block_reason DATA is preserved; old code simply ignores the column.

Modes:
  --dry-run        list boards + triggers that WOULD be dropped (default off)
  --board <slug>   one board (repeatable)
  --all-boards     every board under ~/.hermes/kanban/boards with a kanban.db
  --audit <path>   JSONL audit log (default: alongside the newest board dir)

Every board touched is first snapshotted (file copy) next to its DB. The
script refuses to run against a board whose DB fails PRAGMA integrity_check.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

GUARD_TRIGGERS = (
    "trg_tasks_block_reason_insert",
    "trg_tasks_block_reason_update",
    "trg_tasks_block_reason_clear",
)


def find_boards(kanban_root: Path) -> list[Path]:
    boards_dir = kanban_root / "boards"
    out = []
    if not boards_dir.is_dir():
        return out
    for b in sorted(boards_dir.iterdir()):
        db = b / "kanban.db"
        if b.is_dir() and db.exists():
            out.append(db)
    return out


def snapshot(db: Path) -> Path | None:
    snap = db.with_name(f"{db.name}.pre-rollback-{int(time.time())}")
    if snap.exists():
        return snap
    shutil.copy2(db, snap)
    return snap


def guard_triggers_on(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    return [r[0] for r in rows if r[0] in GUARD_TRIGGERS]


def process(db: Path, dry: bool, audit) -> dict:
    record = {"board": db.parent.name, "db": str(db), "actions": [], "error": None}
    try:
        con = sqlite3.connect(str(db))
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            record["error"] = f"integrity_check={ok}"
            con.close()
            return record
        present = guard_triggers_on(con)
        record["triggers_found"] = present
        if not present:
            record["actions"].append("nothing-to-do")
            con.close()
            return record
        if dry:
            record["actions"].append(f"would-drop {present}")
            con.close()
            return record
        snap = snapshot(db)
        record["snapshot"] = str(snap) if snap else None
        for trg in present:
            con.execute(f"DROP TRIGGER IF EXISTS {trg}")
            record["actions"].append(f"dropped {trg}")
        con.commit()
        # verify
        after = guard_triggers_on(con)
        record["triggers_after"] = after
        ok2 = con.execute("PRAGMA integrity_check").fetchone()[0]
        record["integrity_after"] = ok2
        con.close()
    except Exception as e:  # noqa: BLE001
        record["error"] = f"{type(e).__name__}: {e}"
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--board", action="append", default=[])
    ap.add_argument("--all-boards", action="store_true")
    ap.add_argument("--audit", default=None)
    ap.add_argument("--kanban-root", default=None,
                    help="kanban root override (default: ~/.hermes/kanban)")
    args = ap.parse_args()

    kanban_root = Path(args.kanban_root).expanduser() if args.kanban_root else Path.home() / ".hermes/kanban"
    dbs: list[Path] = []
    if args.all_boards:
        dbs = find_boards(kanban_root)
    for slug in args.board:
        p = kanban_root / "boards" / slug / "kanban.db"
        if p.exists():
            dbs.append(p)
        else:
            print(f"board not found: {slug}", file=sys.stderr)
    if not dbs:
        print("no boards selected (use --all-boards or --board <slug>)", file=sys.stderr)
        return 2

    audit_path = Path(args.audit) if args.audit else kanban_root / "rollback-audit.jsonl"
    rc = 0
    with audit_path.open("a", encoding="utf-8") as audit:
        for db in dbs:
            rec = process(db, args.dry_run, audit)
            audit.write(json.dumps({"ts": int(time.time()), "dry": args.dry_run, **rec}) + "\n")
            status = "ERROR" if rec.get("error") else "OK"
            print(f"{status} {rec['board']}: {rec.get('actions')} {rec.get('error') or ''}")
            if rec.get("error"):
                rc = 1
    print(f"audit: {audit_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
