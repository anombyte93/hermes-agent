"""Read-only sweep: run the completion-contract validator over the Align board's
done runs. Opens the DB with mode=ro; validate_completion only reads git — no
dispatcher/tick/kill path is touched."""
import json
import sqlite3
import sys

sys.path.insert(0, "/home/anombyte/.hermes/hermes-agent/.worktrees/trust-6")
from hermes_cli.kanban_completion import validate_completion, warnings_from  # noqa: E402

DB = "file:/home/anombyte/.hermes/kanban/boards/align-apple-feel-20260913/kanban.db?mode=ro"
conn = sqlite3.connect(DB, uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT r.id AS run_id, r.task_id, r.outcome, r.metadata, t.workspace_path, t.title
      FROM task_runs r JOIN tasks t ON t.id = r.task_id
     WHERE r.outcome = 'completed'
     ORDER BY r.id
""").fetchall()

print(f"done runs on align board: {len(rows)}")
refused = 0
for row in rows:
    md = json.loads(row["metadata"]) if row["metadata"] else {}
    # Stamp what the completer itself added is not evidence — evaluate raw claims.
    problems = validate_completion(md, row["workspace_path"], "warn")
    warns = warnings_from(problems)
    verdict = "PASS" if not warns else f"warn({len(warns)})"
    if warns:
        refused += 1
    codes = [p.code for p in problems]
    print(f"run {row['run_id']:>3} {row['task_id']} {verdict:<10} {codes}")
    for w in warns:
        print(f"        - {w.field}: {w.message[:110]}")
print(f"\nsummary: {len(rows)} done runs, {len(rows)-refused} clean under warn, "
      f"{refused} would be REFUSED under strict (unverifiable claims)")
conn.close()
