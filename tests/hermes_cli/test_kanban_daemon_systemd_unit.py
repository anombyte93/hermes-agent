from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_PATH = REPO_ROOT / "ops/systemd/hermes-kanban-daemon.service"
OPS_NOTE_PATH = REPO_ROOT / "ops/systemd/README.md"


def _unit_text() -> str:
    return UNIT_PATH.read_text(encoding="utf-8")


def test_default_board_daemon_unit_uses_repo_runtime_and_default_board():
    text = _unit_text()

    assert "WorkingDirectory=/home/anombyte/.hermes/hermes-agent" in text
    assert (
        "ExecStart=/home/anombyte/.hermes/hermes-agent/venv/bin/python "
        "-m hermes_cli.main kanban --board default daemon"
    ) in text
    assert "Environment=HERMES_PROFILE=" not in text


def test_default_board_daemon_unit_has_standalone_singleton_runtime_controls():
    text = _unit_text()

    assert "--force" in text
    assert "--interval 30" in text
    assert "--verbose" in text
    assert "--pidfile %t/hermes-kanban-default.pid" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=5" in text
    assert "KillSignal=SIGINT" in text
    assert "TimeoutStopSec=30" in text


def test_ops_note_documents_install_observe_and_reversible_rollback():
    text = OPS_NOTE_PATH.read_text(encoding="utf-8")

    for required in (
        "systemctl --user daemon-reload",
        "systemctl --user enable --now hermes-kanban-daemon.service",
        "systemctl --user status hermes-kanban-daemon.service",
        "journalctl --user -u hermes-kanban-daemon.service",
        "systemctl --user disable --now hermes-kanban-daemon.service",
        "kanban.dispatch_in_gateway",
        "restore",
    ):
        assert required in text
