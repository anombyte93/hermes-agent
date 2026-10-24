"""``hermes kanban list`` shows the resolved board caps and their warnings (#124 display).

The dispatcher resolves the board-scope caps from the default profile's config
(``hermes_cli/kanban_board_caps.py``) but nothing SURFACED them: an operator
watching a ready card not dispatch had to run ``dispatch --dry-run`` to learn
``max_in_progress_by_assignee`` capped it. This is the display half: the
``kanban list`` header (human + ``--json``) and the dashboard ``GET /board``
payload carry the same resolved caps object — values, per-value scope
(managed/user) and the resolver's key-only warnings — computed by ONE call of
the existing resolver, never re-implemented here.

Contract (per the card's known-red controls):
- a cap of ``0`` renders ``0`` (rule 5: zero is a cap, never "uncapped"/blank);
- a key-only resolver warning appears in the list header AND the dashboard;
- managed vs user scope is labelled per value;
- no caps configured renders ``caps: none``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def sticky_profile_home(tmp_path, monkeypatch):
    """HERMES_HOME at a non-default profile; board + default config at the root
    (the #124 topology: board caps live in the DEFAULT profile's config.yaml)."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "evo").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "evo"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return root


def _write_cfg(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    # Push mtime past any coarse-grained cache resolution.
    future = time.time() + 5 + len(body)
    os.utime(path, (future, future))


def _seed_ready(conn, title: str = "ready card", assignee: str = "evo") -> str:
    return kb.create_task(conn, title=title, assignee=assignee)


def _board_lines(caps: dict) -> list[str]:
    """The human header lines for a resolved-caps payload (mirror of the CLI)."""
    from hermes_cli.kanban import _caps_header_lines
    return _caps_header_lines(caps)


# ---------------------------------------------------------------------------
# Known-red control (a): a cap of 0 renders 0, never "uncapped"/blank
# ---------------------------------------------------------------------------


def test_list_header_zero_cap_renders_zero(sticky_profile_home):
    _write_cfg(
        sticky_profile_home / "config.yaml",
        "kanban:\n  max_in_progress: 0\n",
    )
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    text = kc.run_slash("list")
    assert "max_in_progress=0" in text, text
    assert "uncapped" not in text


def test_list_json_zero_cap_value_is_zero_not_null(sticky_profile_home):
    _write_cfg(
        sticky_profile_home / "config.yaml",
        "kanban:\n  max_in_progress: 0\n",
    )
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    payload = json.loads(kc.run_slash("list --json"))
    caps = payload["caps"]
    assert caps["values"]["max_in_progress"] == 0
    assert caps["values"]["max_in_progress"] is not None
    # Scope label present for the configured value.
    assert caps["scope"]["max_in_progress"] == "user"


# ---------------------------------------------------------------------------
# Known-red control (b): a key-only resolver warning reaches both surfaces
# ---------------------------------------------------------------------------


def test_list_header_shows_key_only_warning(sticky_profile_home):
    """An invalid (string-int) cap is dropped and its KEY named in the warning;
    the warning text carries no value read from any profile (rule 6)."""
    _write_cfg(
        sticky_profile_home / "config.yaml",
        "kanban:\n  max_spawn: \"4\"\n  max_in_progress: 3\n",
    )
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    text = kc.run_slash("list")
    assert "warnings" in text
    assert "max_spawn" in text
    # Keys only: the dropped string value must not appear.
    assert '"4"' not in text


def test_list_json_carries_warning(sticky_profile_home):
    _write_cfg(
        sticky_profile_home / "config.yaml",
        "kanban:\n  max_spawn: \"4\"\n",
    )
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    payload = json.loads(kc.run_slash("list --json"))
    assert payload["caps"]["warnings"]
    assert any("max_spawn" in w for w in payload["caps"]["warnings"])


# ---------------------------------------------------------------------------
# Known-red control (c): managed vs user scope labelled per value
# ---------------------------------------------------------------------------


def test_list_header_labels_managed_and_user_scope(sticky_profile_home, tmp_path, monkeypatch):
    _write_cfg(
        sticky_profile_home / "config.yaml",
        "kanban:\n  max_in_progress: 4\n  max_in_progress_by_assignee:\n    evo: 2\n",
    )
    managed = tmp_path / "managed"
    _write_cfg(managed / "config.yaml", "kanban:\n  max_spawn: 6\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()

    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    text = kc.run_slash("list")
    assert "max_in_progress=4 (user)" in text, text
    assert "max_spawn=6 (managed)" in text, text
    assert "zai" not in text  # example naming from the card; none here


def test_list_json_scope_map_labels_both(sticky_profile_home, tmp_path, monkeypatch):
    _write_cfg(
        sticky_profile_home / "config.yaml",
        "kanban:\n  max_in_progress: 4\n",
    )
    managed = tmp_path / "managed"
    _write_cfg(managed / "config.yaml", "kanban:\n  max_spawn: 6\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()

    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    payload = json.loads(kc.run_slash("list --json"))
    assert payload["caps"]["scope"]["max_in_progress"] == "user"
    assert payload["caps"]["scope"]["max_spawn"] == "managed"


# ---------------------------------------------------------------------------
# No caps configured: caps: none
# ---------------------------------------------------------------------------


def test_list_header_no_caps_renders_none(sticky_profile_home):
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    text = kc.run_slash("list")
    assert "caps: none" in text


def test_list_json_no_caps_empty_values(sticky_profile_home):
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    payload = json.loads(kc.run_slash("list --json"))
    assert payload["caps"]["values"] == {}
    assert payload["caps"]["scope"] == {}
    assert payload["caps"]["warnings"] == []


# ---------------------------------------------------------------------------
# Dashboard parity: GET /board returns the same object
# ---------------------------------------------------------------------------


def _plugin_api():
    import importlib.util
    import sys
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_caps_test", plugin_file)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_dashboard_board_returns_same_caps_object(sticky_profile_home, tmp_path, monkeypatch):
    _write_cfg(
        sticky_profile_home / "config.yaml",
        "kanban:\n  max_in_progress: 4\n  max_in_progress_by_assignee:\n    evo: 2\n",
    )
    managed = tmp_path / "managed"
    _write_cfg(managed / "config.yaml", "kanban:\n  max_spawn: 0\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()

    with kbc.connect_closing() as conn:
        _seed_ready(conn)

    mod = _plugin_api()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    client = TestClient(app)
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    caps = r.json()["caps"]
    assert caps["values"]["max_spawn"] == 0  # managed 0 stays 0
    assert caps["scope"]["max_spawn"] == "managed"
    assert caps["scope"]["max_in_progress"] == "user"
    assert caps["values"]["max_in_progress_by_assignee.evo"] == 2

    # SAME object as the CLI JSON (one resolver, two surfaces).
    cli_payload = json.loads(kc.run_slash("list --json"))
    assert cli_payload["caps"] == caps


def test_dashboard_board_caps_key_only_warning(sticky_profile_home):
    _write_cfg(
        sticky_profile_home / "config.yaml",
        "kanban:\n  max_spawn: \"4\"\n",
    )
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    mod = _plugin_api()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    client = TestClient(app)
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    assert any("max_spawn" in w for w in r.json()["caps"]["warnings"])


# ---------------------------------------------------------------------------
# The shared renderer (one place both surfaces use; keeps the header honest)
# ---------------------------------------------------------------------------


def test_caps_header_lines_shapes():
    """Renderer contracts: 0 renders 0; group values render compact; warnings append."""
    lines = _board_lines({
        "values": {
            "max_in_progress": 0,
            "max_in_progress_by_assignee.evo": 2,
            "assignee_groups.gpu": {"max": 3, "members": ["evo", "kimi"]},
        },
        "scope": {
            "max_in_progress": "user",
            "max_in_progress_by_assignee.evo": "user",
            "assignee_groups.gpu": "managed",
        },
        "warnings": ["kanban: ignoring invalid board caps: max_spawn"],
    })
    text = "\n".join(lines)
    assert "max_in_progress=0 (user)" in text
    assert "max_in_progress_by_assignee.evo=2 (user)" in text
    assert "assignee_groups.gpu=max 3 [evo,kimi] (managed)" in text
    assert "warnings: kanban: ignoring invalid board caps: max_spawn" in text
    assert _board_lines({"values": {}, "scope": {}, "warnings": []}) == ["caps: none"]
