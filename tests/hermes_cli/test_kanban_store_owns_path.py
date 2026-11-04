"""Workspace ownership lives behind one store predicate, not per-route SQL.

Every removal route (board removal, task deletion, deferred workspace cleanup)
asks the same question — does this store still own path P? — through
``kanban_db_workspace._store_owns_external_workspace``: any
``task_external_bindings`` row (the session bound a worktree here) plus any
``external_session`` card's ``workspace_path`` (a never-claimed reservation,
including cards created on other boards or moved to custom DB paths).

A never-claimed external card with ``workspace_kind='dir'`` and
``workspace_path=P`` therefore refuses deletion and removal on every route,
not only at cleanup time. A scratch/dir worker merely sharing P survives; P
itself is never removed while the store's cards own it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.kanban.dashboard.plugin_api import router


def _shared_dir(tmp_path: Path) -> Path:
    owned = tmp_path / "external-owner-path"
    owned.mkdir()
    (owned / "evidence.txt").write_text("the session's evidence", encoding="utf-8")
    return owned


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    with kbc.connect_closing(board="default") as conn:
        yield conn


def _reserve(conn, owned: Path) -> str:
    """A never-claimed external card owning ``owned`` with workspace_kind='dir'."""
    return kb.create_task(
        conn, title="reserved external workspace", execution_kind="external_session",
        workspace_kind="dir", workspace_path=str(owned),
    )


def test_remove_board_delete_refuses_never_claimed_external_owner(store, tmp_path):
    owned = _shared_dir(tmp_path)
    kb.create_board("owner-board")
    kb.set_current_board("owner-board")
    with kbc.connect_closing(board="owner-board") as conn:
        _reserve(conn, owned)
        # A scratch worker on ANOTHER board shares P: board deletion must not
        # discard the other board's ownership either (fail closed everywhere).
        with kbc.connect_closing(board="default") as other:
            kb.create_task(other, title="sharing scratch worker", workspace_kind="scratch",
                           workspace_path=str(owned))
        with pytest.raises(ValueError, match="external_binding_history.*before removal"):
            kb.remove_board("owner-board", archive=False)
        assert kb.get_current_board() == "owner-board"
        assert kb.board_dir("owner-board").is_dir()
        # The archive=True control keeps the board (and P) retained.
        assert kb.remove_board("owner-board", archive=True)["action"] == "archived"
    assert owned.is_dir()
    assert (owned / "evidence.txt").read_text(encoding="utf-8") == "the session's evidence"


def test_delete_task_refuses_never_claimed_external_owner(store, tmp_path):
    owned = _shared_dir(tmp_path)
    tid = _reserve(store, owned)
    before = "\n".join(store.iterdump())
    with pytest.raises(ValueError, match="external_workspace_retained"):
        kb.delete_task(store, tid)
    assert "\n".join(store.iterdump()) == before
    assert kb.get_task(store, tid) is not None
    assert owned.is_dir()


def test_delete_archived_task_refuses_never_claimed_external_owner(store, tmp_path):
    owned = _shared_dir(tmp_path)
    tid = _reserve(store, owned)
    assert kb.archive_task(store, tid)
    before = "\n".join(store.iterdump())
    with pytest.raises(ValueError, match="external_workspace_retained"):
        kb.delete_archived_task(store, tid)
    assert "\n".join(store.iterdump()) == before
    assert owned.is_dir()
    # The control: a sharing worker's archive never removes the owned path.
    worker = kb.create_task(store, title="sharing worker", workspace_kind="scratch",
                            workspace_path=str(owned))
    assert kb.archive_task(store, worker)
    assert owned.is_dir()


def test_dashboard_delete_task_refuses_never_claimed_external_owner(store, tmp_path):
    owned = _shared_dir(tmp_path)
    tid = _reserve(store, owned)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app, raise_server_exceptions=False).delete(f"/tasks/{tid}")
    assert response.status_code == 400, response.text
    assert "external_workspace_retained" in response.text
    assert kb.get_task(store, tid) is not None
    assert owned.is_dir()


def test_plain_card_beside_never_claimed_external_card_deletes(store, tmp_path):
    """A plain card on a store that also holds a never-claimed external card
    still hard-deletes: the ownership question is scoped to the deleted card
    (``task_id``), not the whole store. Kills the ``task_arm_scope_off``
    mutant (the never-claimed arm loses its ``id = ?`` filter), which would
    refuse every plain card sharing the store."""
    owned = _shared_dir(tmp_path)
    _reserve(store, owned)
    plain = kb.create_task(store, title="plain neighbour")
    assert kb.delete_task(store, plain) is True
    assert kb.get_task(store, plain) is None
    # The external card itself and its reservation survive untouched.
    after = "\n".join(store.iterdump())
    assert "reserved external workspace" in after
    assert owned.is_dir()


def test_delete_task_fails_closed_when_store_unreadable(store, tmp_path, monkeypatch):
    """``_delete_task_relations`` must treat an unreadable store (None) as
    ownership and refuse, never delete. Kills the ``none_fail_open_dtr``
    mutant (``is not False`` -> truthiness), which deletes the card while
    ownership is unknown."""
    from hermes_cli import kanban_db_workspace as kbw
    import sqlite3 as _sqlite3

    def _unreadable(conn, task_id=None):
        raise _sqlite3.OperationalError("injected unreadable store")

    tid = kb.create_task(store, title="card on a store that turns unreadable")
    before = "\n".join(store.iterdump())
    monkeypatch.setattr(kbw, "_external_workspace_paths", _unreadable)
    with pytest.raises(ValueError, match="external_workspace_retained"):
        kb.delete_task(store, tid)
    assert "\n".join(store.iterdump()) == before
    assert kb.get_task(store, tid) is not None


def test_remove_board_fails_closed_when_ownership_unknown(store, tmp_path, monkeypatch):
    """``remove_board``'s own-store history read (no path, no task) returning
    None (unreadable) must refuse the hard delete and keep the board, even
    though every OTHER store verifies clean. Kills the ``none_fail_open_rb``
    mutant (``is not False`` -> truthiness): it would treat unknown ownership
    as no-ownership, sail past the history gate, and delete the board."""
    from hermes_cli import kanban_db_workspace as kbw

    real = kbw._store_owns_external_workspace

    def _unreadable_history(conn, path=None, *, task_id=None):
        # Only remove_board's own-store history call (no path, no task) is
        # unreadable; path-scoped reads on every store stay healthy.
        if path is None and task_id is None:
            return None
        return real(conn, path, task_id=task_id)

    kb.create_board("mystery")
    monkeypatch.setattr(kbw, "_store_owns_external_workspace", _unreadable_history)
    with pytest.raises(ValueError, match="external_binding_history"):
        kb.remove_board("mystery", archive=False)
    assert kb.board_dir("mystery").is_dir()


def test_cross_store_check_fails_closed_when_current_unreadable(store, tmp_path, monkeypatch):
    """``_externally_owned_workspace`` with a live conn must fail closed
    (True) when the CURRENT store cannot be read, even with every other
    store verifying clean. Kills the ``none_fail_open_ewp_cur`` mutant
    (``is not False`` -> truthiness), which would fall through to the
    other-stores loop and answer False."""
    from hermes_cli import kanban_db_workspace as kbw

    real = kbw._store_owns_external_workspace

    def _unreadable_current(conn, path=None, *, task_id=None):
        if conn is store:  # only the live current-store read is unreadable
            return None
        return real(conn, path, task_id=task_id)

    target = tmp_path / "some-path"
    target.mkdir()
    monkeypatch.setattr(kbw, "_store_owns_external_workspace", _unreadable_current)
    assert kbw._externally_owned_workspace(store, str(target)) is True
