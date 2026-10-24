"""Positive and negative controls for physical worker placement."""

import pytest

from hermes_cli.kanban_host import require_execution_host


def test_required_host_accepts_actual_evo():
    require_execution_host({"kanban": {"execution_host": "evo"}}, "evo")


def test_evo_provider_on_archie_does_not_satisfy_host():
    with pytest.raises(RuntimeError, match="No local fallback was started"):
        require_execution_host({"kanban": {"execution_host": "evo"},
                                "model": {"provider": "evo"}}, "Archie")


def test_unconfigured_preserves_upstream_behaviour():
    require_execution_host({}, "Archie")


@pytest.mark.parametrize("value", ["", "  ", False, 123, []])
def test_invalid_host_fails_closed(value):
    with pytest.raises(ValueError):
        require_execution_host({"kanban": {"execution_host": value}}, "evo")


def test_case_is_normalised_but_other_hosts_are_not_aliases():
    require_execution_host({"kanban": {"execution_host": " EVO "}}, "Evo")
    with pytest.raises(RuntimeError):
        require_execution_host({"kanban": {"execution_host": "evo"}}, "evo-other")


def test_default_spawn_checks_host_before_launch(monkeypatch):
    from hermes_cli import kanban_db, config
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"execution_host": "evo"}})
    monkeypatch.setattr("hermes_cli.kanban_host.socket.gethostname", lambda: "Archie")
    # None cannot supply an assignee or create a workspace. Host validation
    # must reject before even reading the task, not after Popen.
    with pytest.raises(RuntimeError, match="No local fallback"):
        kanban_db._default_spawn(None, "/nonexistent")


def test_dispatch_refuses_before_touching_board(monkeypatch):
    from hermes_cli import kanban_db, config
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"execution_host": "evo"}})
    monkeypatch.setattr("hermes_cli.kanban_host.socket.gethostname", lambda: "Archie")
    with pytest.raises(RuntimeError, match="No local fallback"):
        kanban_db.dispatch_once(None, max_spawn=1)
