"""Card-creation kanban settings fall back to the ROOT config when the profile is silent.

Boards live under the Hermes root and are shared by every profile, but card creation
reads settings from whichever profile created the card. On Archie a bare `hermes`
follows the sticky active profile (astra), so `kanban.max_runtime_by_assignee` and
`kanban.require_known_assignee` set in the root `~/.hermes/config.yaml` (the one home
the board daemons read with `-p default`) were invisible to cards created from astra.
A profile that sets the key itself still wins.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """HOME with a root ~/.hermes and an active profile 'astra' as HERMES_HOME."""
    root = tmp_path / ".hermes"
    prof = root / "profiles" / "astra"
    for name in ("evo", "astra"):
        _write(root / "profiles" / name / "config.yaml", "model:\n  default: x\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    for mod in list(sys.modules):
        if mod.startswith("hermes_cli") or mod == "hermes_constants":
            del sys.modules[mod]
    return root, prof


def test_runtime_default_comes_from_root_when_profile_is_silent(profile_home):
    root, prof = profile_home
    _write(root / "config.yaml", "kanban:\n  max_runtime_by_assignee:\n    evo: 5400\n")
    _write(prof / "config.yaml", "kanban:\n  max_in_progress: 24\n")
    from hermes_cli import kanban_db_dispatch as kbd

    assert kbd.default_max_runtime_for("evo") == 5400


def test_profile_value_wins_over_root(profile_home):
    root, prof = profile_home
    _write(root / "config.yaml", "kanban:\n  max_runtime_by_assignee:\n    evo: 5400\n")
    _write(prof / "config.yaml", "kanban:\n  max_runtime_by_assignee:\n    evo: 600\n")
    from hermes_cli import kanban_db_dispatch as kbd

    assert kbd.default_max_runtime_for("evo") == 600


def test_require_known_assignee_comes_from_root_when_profile_is_silent(profile_home):
    root, prof = profile_home
    _write(root / "config.yaml", "kanban:\n  require_known_assignee: true\n")
    _write(prof / "config.yaml", "kanban:\n  max_in_progress: 24\n")
    from hermes_cli import kanban_db as kb

    assert kb.require_known_assignee_enabled() is True


def test_profile_can_switch_require_known_assignee_off(profile_home):
    root, prof = profile_home
    _write(root / "config.yaml", "kanban:\n  require_known_assignee: true\n")
    _write(prof / "config.yaml", "kanban:\n  require_known_assignee: false\n")
    from hermes_cli import kanban_db as kb

    assert kb.require_known_assignee_enabled() is False


def test_unreadable_root_config_is_ignored(profile_home):
    root, prof = profile_home
    _write(root / "config.yaml", "kanban: [not: a mapping\n")
    _write(prof / "config.yaml", "kanban:\n  max_in_progress: 24\n")
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    assert kb.require_known_assignee_enabled() is False
    assert kbd.default_max_runtime_for("evo") is None
