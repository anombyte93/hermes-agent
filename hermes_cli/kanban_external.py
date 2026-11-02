"""CLI edge for external-session claims. No process or reservation-owner effects."""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_external as external
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_output import _err, _print_json


def _claim(args, conn):
    try:
        incarnation = json.loads(args.incarnation)
    except (ValueError, TypeError):
        raise ValueError("incarnation must be a JSON object") from None
    return external.external_claim(conn, args.task, board=kb.get_current_board(),
        owner_host=args.owner_host, incarnation=incarnation, worktree=args.worktree,
        reservation_id=args.reservation_id, reservation_token=args.reservation_token)


def _complete(args, conn):
    return external.external_complete(conn, args.task, args.run_id,
        reservation_token=args.reservation_token, summary=args.summary, result=args.result, review=args.review)


def _release(args, conn):
    return external.external_release(conn, args.task, args.run_id,
        reservation_token=args.reservation_token, reason=args.reason)


def _changes(args, conn):
    return external.external_request_changes(conn, args.task, args.run_id,
        reservation_token=args.reservation_token, reason=args.reason)


def _handover(args, conn):
    try:
        incarnation = json.loads(args.incarnation)
    except (ValueError, TypeError):
        raise ValueError("incarnation must be a JSON object") from None
    return external.external_handover(conn, args.task, args.run_id,
        reservation_token=args.reservation_token, owner_host=args.owner_host, incarnation=incarnation,
        worktree=args.worktree, reservation_id=args.reservation_id,
        new_reservation_token=args.new_reservation_token, evidence=args.evidence)


_HANDLERS = {"claim": _claim, "status": lambda args, conn: external.external_status(conn, args.task),
             "complete": _complete, "release": _release, "request-changes": _changes, "handover": _handover}


def external_command(args) -> int:
    handler = _HANDLERS.get(getattr(args, "external_action", None))
    if handler is None:
        return _err("kanban external: choose claim, status, complete, request-changes, release or handover", 2)
    try:
        with connect_closing() as conn:
            result = handler(args, conn)
    except ValueError as exc:
        if getattr(args, "json", False):
            _print_json({"state": "refused", "reason": str(exc)})
            return 1
        return _err(str(exc))
    _print_json(result)
    return 0
