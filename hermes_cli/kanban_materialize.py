"""CLI boundary for canonical plan materialization. No session-owner effects."""
from __future__ import annotations

from pathlib import Path

from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_materialize import materialize_task
from hermes_cli.kanban_output import _err, _print_json


def materialize_command(args) -> int:
    try:
        body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else None
        with connect_closing() as conn:
            result = materialize_task(conn, project=args.project, plan_revision=args.plan_revision,
                proposed_task_id=args.proposed_id, intent_fingerprint=args.fingerprint,
                title=args.title, body=body, parent_proposed=args.parent_proposed,
                maps_from=args.maps_from, execution_kind=args.execution_kind)
    except (OSError, UnicodeError, ValueError) as exc:
        if args.json:
            _print_json({"state": "refused", "reason": str(exc)})
            return 1
        return _err(f"kanban materialize: {exc}")
    if args.json:
        _print_json(result)
    else:
        print(f"{result['state']}: {result['task_id']}" +
              (f" ({result['reason']})" if "reason" in result else ""))
    return 3 if result["state"] == "conflict" else 0
