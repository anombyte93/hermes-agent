"""CLI boundary for canonical plan materialization. No session-owner effects."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_materialize import check_materialization, materialize_task, migrate_materializations
from hermes_cli.kanban_output import _err, _print_json
from hermes_cli.sqlite_safe_read import connect_tracked


@contextmanager
def _check_connection():
    """Never initialise, migrate or repair the source board during preflight."""
    from hermes_cli import kanban_db as kb

    path = kb.kanban_db_path()
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as snapshot:
        snapshot.row_factory = sqlite3.Row
        if path.exists():
            with closing(connect_tracked(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                source.backup(snapshot)
        else:
            snapshot.executescript(kb.SCHEMA_SQL)
        migrate_materializations(snapshot)
        yield snapshot


def materialize_command(args) -> int:
    check = args.kanban_action == "materialize-check"
    try:
        body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else None
        with (_check_connection() if check else connect_closing()) as conn:
            options = {}
            if check:
                pending = {}
                for value in args.pending_parent_proposed:
                    proposed, sep, task_id = value.partition("=")
                    if not proposed or (sep and not task_id) or proposed in pending:
                        raise ValueError("invalid_pending_parent")
                    pending[proposed] = task_id if sep else None
                options["pending_parents"] = pending
                prior_file = getattr(args, "prior_rows_file", None)
                if prior_file:
                    options["prior_rows"] = json.loads(Path(prior_file).read_text(encoding="utf-8"))
            operation = check_materialization if check else materialize_task
            result = operation(conn, project=args.project, plan_revision=args.plan_revision,
                proposed_task_id=args.proposed_id, intent_fingerprint=args.fingerprint,
                title=args.title, body=body, parent_proposed=args.parent_proposed,
                maps_from=args.maps_from, execution_kind=args.execution_kind, **options)
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
        reason = "board_unreadable" if isinstance(exc, sqlite3.Error) else str(exc)
        if args.json:
            _print_json({"state": "refused", "reason": reason})
            return 1
        return _err(f"kanban materialize: {reason}")
    if args.json:
        _print_json(result)
    else:
        print(f"{result['state']}: {result['task_id']}" +
              (f" ({result['reason']})" if "reason" in result else ""))
    return 3 if result["state"] == "conflict" else 0
