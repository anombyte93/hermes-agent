"""Amending the body/title of a card that is not yet done (#180).

``edit <id> --result`` stays in ``kanban_db.edit_completed_task_result``
(done-card recovery). This sibling owns the OTHER mode: correcting the brief
of an open card. The status gate lives inside ``write_txn`` so a card that
completes between check and write is refused, and the audit event carries
SHA-256 hashes and lengths — never the body text.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional

from hermes_cli.kanban_db import (
    VALID_STATUSES,
    _append_event,
    _task_status,
)
from hermes_cli.kanban_db_connect import write_txn

# Every status except the two frozen ones: ``done`` keeps only the recovery
# fields (``--result``), ``archived`` is frozen outright.
_AMENDABLE_STATUSES = frozenset(VALID_STATUSES) - {"done", "archived"}


class RefusedEditError(ValueError):
    """The card's state refuses an amend (unknown id, done or archived).

    A ``ValueError`` subclass so both CLI and DB callers treat a refusal as a
    usage-level outcome, while ``.reason`` carries the targeted message.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def amend_open_task(
    conn: sqlite3.Connection, task_id: str, *,
    body: Optional[str] = None, title: Optional[str] = None,
) -> bool:
    """Rewrite ``body``/``title`` of a card that is not ``done``/``archived``.

    One audited event (``body_edited``) with the changed field names, old/new
    body SHA-256 and lengths, the status at edit time and ``current_run_id``
    when running — never the text. True when something changed;
    :class:`RefusedEditError` for an unknown id, a frozen status or a no-op
    (identical body and title: refused without an event or a notify),
    ``ValueError`` for blank input or when neither field is given.
    ``notify_task_updated`` fires after commit with the changed field names only.
    """
    if body is None and title is None:
        raise ValueError("nothing to edit: pass --body-file and/or --title")
    if body is not None and not body.strip():
        raise ValueError("body cannot be blank")
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")

    changed_fields: list[str] = []
    with write_txn(conn):
        # The status read happens after BEGIN IMMEDIATE took the write lock:
        # a completion that commits before this point is visible here and
        # refused; one that lands after cannot interleave (single writer).
        status = _task_status(conn, task_id)
        if status is None:
            raise RefusedEditError(f"unknown task {task_id}")
        if status == "done":
            raise RefusedEditError(
                f"{task_id} is done; a done card keeps only its recovery "
                "fields (edit <id> --result ...)"
            )
        if status == "archived":
            raise RefusedEditError(f"{task_id} is archived; archived cards are frozen")

        row = conn.execute(
            "SELECT title, body, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        old_title, old_body, current_run_id = row["title"], row["body"], row["current_run_id"]

        sets: list[str] = []
        params: list[object] = []
        changed_fields: list[str] = []
        payload: dict[str, object] = {"status": status}

        if body is not None and body != (old_body or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
            payload["old_body_sha256"] = _sha256_text(old_body or "")
            payload["new_body_sha256"] = _sha256_text(body)
            payload["old_body_len"] = len(old_body or "")
            payload["new_body_len"] = len(body)
        if title is not None and title.strip() != (old_title or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        payload["fields"] = changed_fields

        if not changed_fields:
            # A no-op is refused, never returned as quiet success: the caller
            # must not mistake "nothing changed" for a recorded edit. The id is
            # NOT repeated here — the CLI prefix already names the card.
            raise RefusedEditError(
                "nothing changed (body and title already current)"
            )

        if current_run_id:
            payload["current_run_id"] = int(current_run_id)

        params.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params),
        )
        _append_event(conn, task_id, "body_edited", payload)
    # Late-bound through the facade so the monkeypatch seam stays live
    # (siblings patch where production reads; a module-level import dead-ends it).
    from hermes_cli import kanban_db as _kb

    _kb.notify_task_updated(conn, task_id, changed_fields)
    return True
