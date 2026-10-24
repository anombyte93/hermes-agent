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
    payload = json.loads(kc.run_slash("list --json --with-caps"))
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
    payload = json.loads(kc.run_slash("list --json --with-caps"))
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
    payload = json.loads(kc.run_slash("list --json --with-caps"))
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
    payload = json.loads(kc.run_slash("list --json --with-caps"))
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
    cli_payload = json.loads(kc.run_slash("list --json --with-caps"))
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


# ---------------------------------------------------------------------------
# #147 review r1 P1: plain ``list --json`` stays the bare task array
# ---------------------------------------------------------------------------

_WORKTREE = Path(__file__).resolve().parents[2]
_MONITOR = (_WORKTREE / "optional-skills" / "creative" / "kanban-video-orchestrator"
            / "scripts" / "monitor.py")


def test_list_json_plain_is_bare_task_array_byte_compatible(sticky_profile_home):
    """Plain ``--json`` is byte-identical to the pre-caps output: the bare task
    array, non-ASCII kept as-is, and no caps anywhere in it."""
    _write_cfg(sticky_profile_home / "config.yaml", "kanban:\n  max_in_progress: 0\n")
    with kbc.connect_closing() as conn:
        _seed_ready(conn, title="café card")
        tasks = kb.list_tasks(conn)
    raw = kc.run_slash("list --json")
    expected = json.dumps([kc._task_to_dict(t) for t in tasks], indent=2, ensure_ascii=False) + "\n"
    assert raw == expected.rstrip("\n")  # run_slash rstrips captured stdout
    assert isinstance(json.loads(raw), list)
    assert "caps" not in raw and "max_in_progress" not in raw


def test_list_json_with_caps_is_the_opt_in_envelope(sticky_profile_home):
    _write_cfg(sticky_profile_home / "config.yaml", "kanban:\n  max_in_progress: 0\n")
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    payload = json.loads(kc.run_slash("list --json --with-caps"))
    assert set(payload) == {"tasks", "caps"}
    assert [t["title"] for t in payload["tasks"]] == ["ready card"]
    assert payload["caps"]["values"]["max_in_progress"] == 0


def _load_monitor():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("kanban_video_monitor_under_test", _MONITOR)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _real_cli_runner(home: Path, extra: tuple = ()):
    """A ``subprocess.run`` stand-in that runs the REAL ``hermes`` CLI from this
    worktree (``python -m hermes_cli.main``) against an isolated home."""
    import subprocess
    import sys

    env = dict(os.environ)
    env.update({"PYTHONPATH": str(_WORKTREE), "HERMES_HOME": str(home), "HOME": str(home.parent)})
    for var in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_MANAGED_DIR"):
        env.pop(var, None)
    real_run = subprocess.run

    def run(argv, **kw):
        assert argv[0] == "hermes", argv
        argv = [sys.executable, "-m", "hermes_cli.main", *argv[1:]]
        if "--json" in argv:
            argv = argv + list(extra)
        kw.pop("check", None)
        return real_run(argv, env=env, cwd=str(_WORKTREE), timeout=300, check=False, **kw)

    return run


@pytest.mark.parametrize("extra", [(), ("--with-caps",)], ids=["plain-json", "with-caps-envelope"])
def test_monitor_kanban_list_parses_real_cli_json(tmp_path, monkeypatch, extra):
    """r1 P1 regression control: monitor.py's ``kanban_list`` parser run against
    the real CLI JSON returns the board's tasks (a dict envelope on plain
    ``--json`` made it fall back to the text parser and report zero)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    _write_cfg(home / "config.yaml", "kanban:\n  max_in_progress: 2\n")
    monitor = _load_monitor()
    run = _real_cli_runner(home, extra)
    for title in ("Shot 1", "Shot 2"):
        created = run(["hermes", "kanban", "create", title, "--assignee", "dev", "--tenant", "vid"],
                      capture_output=True, text=True)
        assert created.returncode == 0, created.stderr
    monkeypatch.setattr(monitor.subprocess, "run", run)
    rows = monitor.kanban_list("vid")
    assert sorted(r["title"] for r in rows) == ["Shot 1", "Shot 2"], rows
    assert all(r["id"].startswith("t_") and r["tenant"] == "vid" for r in rows), rows


# ---------------------------------------------------------------------------
# #147 review r1 P2 (finding 2): warnings print even when no cap value resolves
# ---------------------------------------------------------------------------


def test_list_header_warning_only_config_prints_warnings(sticky_profile_home):
    """The reviewer's case: the ONLY cap is invalid and memory sampling is
    unavailable (conftest stubs it to ``{}``), so ``values`` is empty and the
    resolver's warning is the only information. The header must print
    ``caps: none`` AND that warning, never swallow it."""
    _write_cfg(sticky_profile_home / "config.yaml", "kanban:\n  max_spawn: \"4\"\n")
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    lines = kc.run_slash("list").splitlines()
    assert "caps: none" in lines, lines
    warning_lines = [line for line in lines if line.startswith("warnings: ")]
    assert warning_lines, lines
    assert any("max_spawn" in line for line in warning_lines), warning_lines


def test_caps_header_lines_warning_only_payload():
    assert _board_lines({
        "values": {}, "scope": {},
        "warnings": ["kanban: ignoring invalid board caps: max_spawn"],
    }) == ["caps: none", "warnings: kanban: ignoring invalid board caps: max_spawn"]


# ---------------------------------------------------------------------------
# #147 review r1 P2 (finding 3): no cap number or provider name, in any form
# ---------------------------------------------------------------------------
#
# Rule 6: warnings name KEYS only. The old assertions excluded the quoted text
# ``"4"`` and an unseeded ``zai``, so a warning reading ``max_spawn=4`` passed.
# These controls seed distinctive synthetic private values (cap numbers that
# never occur elsewhere, provider ids in the same configs) into a
# warning-only configuration, so the whole caps surface must be digit-free,
# and check it with ONE checker that a planted leaking resolver must fail.

import re

_PRIVATE_NUMBERS = ("58213", "40917", "39012", "61027", "70455", "92741", "83306")
_SEEDED_PROVIDERS = ("zai", "glm", "openrouter", "anthropic")

# Default profile: every board cap invalid (dropped and named by key), each
# carrying a distinctive number or a provider-named ${...} reference, beside
# provider ids elsewhere in the same file.
_DEFAULT_PRIVATE_CFG = """\
model:
  provider: zai
  default: glm-coder
fallback_providers:
  - openrouter
kanban:
  max_spawn: "58213"
  max_in_progress: "${ZAI_MAX_IN_PROGRESS}"
  max_in_progress_per_profile: 61027.5
  default_assignee: 70455
  max_in_progress_by_assignee:
    evo: "40917"
  assignee_groups:
    gpu:
      max: "39012"
      members: [evo]
"""

# The invoking (non-default) profile: valid caps that differ from the board's,
# so the resolver emits its mismatch warning — which must name keys only.
_INVOKING_PRIVATE_CFG = """\
model:
  provider: anthropic
kanban:
  max_spawn: 92741
  max_in_progress: 83306
"""


def _provider_ids() -> list:
    """Every provider id and alias Hermes knows (registry, bespoke, aliases)."""
    from hermes_cli import auth
    ids = (set(auth.PROVIDER_REGISTRY) | set(auth._REGISTRY_PLUGIN_SKIP)
           | set(auth._PROVIDER_ALIASES) | set(auth._PROVIDER_ALIASES.values()))
    assert set(_SEEDED_PROVIDERS) - {"glm"} <= ids and "glm" in auth._PROVIDER_ALIASES
    return sorted(ids)


def _token_hits(text: str, tokens) -> list:
    """Tokens found in ``text`` as whole tokens in any case (``_``, ``.``,
    ``$``, ``{`` and spaces all separate: ``${ZAI_X}`` hits ``zai``)."""
    return [tok for tok in tokens
            if re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", text, re.IGNORECASE)]


def _assert_caps_surface_private(surface: str, default_cfg: Path) -> None:
    """THE privacy checker: the caps surface (header lines or caps JSON) of a
    warning-only configuration carries no digit at all (so no cap number in
    any form: quoted, ``key=N``, ``key N``) and no provider id. The default
    config path the mismatch warning names is the only scrubbed text."""
    scrubbed = surface.replace(str(default_cfg), "<default-config>")
    digits = re.findall(r"\d+", scrubbed)
    assert not digits, f"cap number(s) {digits} leaked into the caps surface: {scrubbed!r}"
    hits = _token_hits(scrubbed, _provider_ids())
    assert not hits, f"provider id(s) {hits} leaked into the caps surface: {scrubbed!r}"


def _assert_no_seeded_private_value(text: str) -> None:
    """Whole-output check (task rows included): no seeded number or provider."""
    numbers = [n for n in _PRIVATE_NUMBERS if re.search(r"(?<![0-9a-f])" + n + r"(?![0-9a-f])", text)]
    assert not numbers, f"seeded private cap number(s) {numbers} leaked: {text!r}"
    hits = _token_hits(text, _SEEDED_PROVIDERS)
    assert not hits, f"seeded provider id(s) {hits} leaked: {text!r}"


def _seed_private_configs(root: Path) -> Path:
    _write_cfg(root / "config.yaml", _DEFAULT_PRIVATE_CFG)
    _write_cfg(root / "profiles" / "evo" / "config.yaml", _INVOKING_PRIVATE_CFG)
    from hermes_cli import kanban_board_caps as kbcaps
    default_cfg = kbcaps.default_profile_config_path()
    assert default_cfg == root / "config.yaml"
    with kbc.connect_closing() as conn:
        _seed_ready(conn)
    return default_cfg


def _human_caps_surface(text: str) -> str:
    return "\n".join(line for line in text.splitlines()
                     if line.startswith(("caps:", "warnings:")))


def _private_outputs(root: Path) -> dict:
    default_cfg = _seed_private_configs(root)
    human = kc.run_slash("list")
    payload = json.loads(kc.run_slash("list --json --with-caps"))
    return {"default_cfg": default_cfg, "human": human, "payload": payload,
            "plain": kc.run_slash("list --json")}


def _assert_private(out: dict) -> None:
    surface = _human_caps_surface(out["human"])
    _assert_caps_surface_private(surface, out["default_cfg"])
    _assert_no_seeded_private_value(out["human"].replace(str(out["default_cfg"]), "<default-config>"))
    caps_json = json.dumps(out["payload"]["caps"])
    _assert_caps_surface_private(caps_json, out["default_cfg"])
    _assert_no_seeded_private_value(json.dumps(out["payload"]).replace(str(out["default_cfg"]), ""))
    _assert_no_seeded_private_value(out["plain"])


def test_caps_privacy_no_number_or_provider_in_any_form(sticky_profile_home):
    out = _private_outputs(sticky_profile_home)
    # Not vacuous: both resolver warnings (invalid keys, profile mismatch)
    # reach the human header and the JSON, naming the keys.
    surface = _human_caps_surface(out["human"])
    assert surface.splitlines()[0] == "caps: none", surface
    assert surface.count("warnings: ") == 2, surface
    for key in ("max_spawn", "max_in_progress_per_profile", "default_assignee",
                "max_in_progress_by_assignee.evo", "assignee_groups.gpu"):
        assert key in surface, (key, surface)
    assert "invoking profile caps differ" in surface, surface
    assert len(out["payload"]["caps"]["warnings"]) == 2, out["payload"]["caps"]
    assert all(w.startswith("kanban: ") for w in out["payload"]["caps"]["warnings"])
    _assert_private(out)


def test_caps_privacy_dashboard_payload(sticky_profile_home):
    default_cfg = _seed_private_configs(sticky_profile_home)
    mod = _plugin_api()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    r = TestClient(app).get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    caps = r.json()["caps"]
    assert caps["values"] == {} and len(caps["warnings"]) == 2, caps
    _assert_caps_surface_private(json.dumps(caps), default_cfg)


def test_caps_display_keeps_each_resolver_warning_whole():
    """Surfaced by the privacy control: the resolver joins its lines with
    "; " and its invalid-keys line itself contains "; ", so splitting on
    every ";" cut one warning into fragments (two warnings showed as four)."""
    from hermes_cli import kanban_board_caps as kbcaps
    joined = ("kanban: ignoring invalid board caps (literal integers and names only; "
              "a ${...} reference is never expanded): max_spawn; "
              "kanban: invoking profile caps differ from default profile caps (max_spawn); "
              "board caps taken from the default profile config (<path>)")
    assert kbcaps.caps_display(kbcaps.BoardCaps({}, warning=joined))["warnings"] == [
        "kanban: ignoring invalid board caps (literal integers and names only; "
        "a ${...} reference is never expanded): max_spawn",
        "kanban: invoking profile caps differ from default profile caps (max_spawn); "
        "board caps taken from the default profile config (<path>)",
    ]


_LEAKS = {
    "key=number": "kanban: dropped max_spawn=58213",
    "key number": "kanban: dropped max_spawn 58213",
    "number only": "kanban: invoking profile has 92741",
    "provider": "kanban: board caps came from the zai profile",
    "provider caps": "kanban: OpenRouter caps differ",
    "reference": "kanban: max_in_progress is ${ZAI_MAX_IN_PROGRESS}",
}


@pytest.mark.parametrize("leak", list(_LEAKS.values()), ids=list(_LEAKS))
def test_caps_privacy_checker_rejects_planted_leaking_resolver(sticky_profile_home, monkeypatch, leak):
    """Known-red control: a resolver that leaks a private value WITHOUT quotes
    must fail the same checker the real resolver passes. The old assertion
    (``'"4"' not in text``) accepted every one of these leaks."""
    import dataclasses
    from hermes_cli import kanban_board_caps as kbcaps

    real = kbcaps.read_board_caps

    def leaky_read_board_caps():
        caps = real()
        return dataclasses.replace(caps, warning="; ".join(w for w in (caps.warning, leak) if w))

    monkeypatch.setattr(kbcaps, "read_board_caps", leaky_read_board_caps)
    out = _private_outputs(sticky_profile_home)
    assert leak in out["human"], out["human"]  # the plant took effect
    for number in _PRIVATE_NUMBERS:  # the old, quote-only exclusion is blind to it
        assert f'"{number}"' not in out["human"]
    with pytest.raises(AssertionError):
        _assert_caps_surface_private(_human_caps_surface(out["human"]), out["default_cfg"])
    with pytest.raises(AssertionError):
        _assert_caps_surface_private(json.dumps(out["payload"]["caps"]), out["default_cfg"])
    with pytest.raises(AssertionError):
        _assert_private(out)


# ---------------------------------------------------------------------------
# #147 review r1 P2 (finding 4): the shipped dashboard bundle renders the caps
# ---------------------------------------------------------------------------
#
# The API tests above stop at the payload. These run the SHIPPED bundle
# (plugins/kanban/dashboard/dist/index.js) in node with a stub SDK, let the
# kanban page load a /board response carrying a caps object, find the
# BoardCapsStrip element the page actually renders, render it, and assert on
# the visible text. Known-red controls plant a strip that returns null, hides
# warnings, shows zero as "uncapped" or drops the scope label; each must FAIL.

import shutil
import subprocess

_BUNDLE = _WORKTREE / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"

_STRIP_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const [, , jsPath, capsPath] = process.argv;
const capsSpec = JSON.parse(fs.readFileSync(capsPath, "utf8"));
let slots = [], idx = 0, effects = [];
function changed(prev, deps) {
  return !prev || !deps || deps.length !== prev.length
    || deps.some(function (d, k) { return !Object.is(d, prev[k]); });
}
const hooks = {
  useState(init) {
    const i = idx++;
    if (!(i in slots)) slots[i] = typeof init === "function" ? init() : init;
    return [slots[i], function (v) { slots[i] = typeof v === "function" ? v(slots[i]) : v; }];
  },
  useEffect(fn, deps) {
    const i = idx++;
    if (changed(slots[i], deps)) { slots[i] = deps; effects.push(fn); }
  },
  useMemo(fn, deps) {
    const i = idx++;
    if (!slots[i] || changed(slots[i].deps, deps)) slots[i] = { deps: deps, value: fn() };
    return slots[i].value;
  },
  useCallback(fn, deps) { return hooks.useMemo(function () { return fn; }, deps); },
  useRef(init) { const i = idx++; if (!(i in slots)) slots[i] = { current: init }; return slots[i]; },
};
function createElement(type, props) {
  const children = Array.prototype.slice.call(arguments, 2);
  return { type: type, props: Object.assign({}, props || {}, { children: children }) };
}
class Component { constructor(props) { this.props = props; this.state = {}; } setState() {} }
const React = Object.assign({ createElement: createElement, Component: Component }, hooks);
const components = {};
["Card", "CardContent", "Badge", "Button", "Input", "Label", "Select", "SelectOption"]
  .forEach(function (n) { components[n] = { [n]: function () { return null; } }[n]; });
function respond(url) {
  const path = url.split("?")[0];
  if (path.endsWith("/config")) return { render_markdown: true };
  if (path.endsWith("/boards")) return { boards: [{ slug: "default", name: "default" }], current: "default" };
  if (path.endsWith("/board")) {
    const board = { columns: [], tenants: [], assignees: [], latest_event_id: 0 };
    if (capsSpec.mode === "present") board.caps = capsSpec.caps;
    return board;
  }
  return {};
}
const SDK = {
  React: React, hooks: hooks, components: components,
  utils: { cn: function () { return ""; }, timeAgo: function () { return ""; } },
  fetchJSON: function (url) { return Promise.resolve(respond(url)); },
  buildWsUrl: function () { return new Promise(function () {}); },
};
let page = null;
const store = { "hermes.kanban.selectedBoard": "default" };
const noop = function () {};
const window = {
  __HERMES_PLUGIN_SDK__: SDK,
  __HERMES_PLUGINS__: { register: function (name, comp) { if (name === "kanban") page = comp; } },
  localStorage: {
    getItem: function (k) { return k in store ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
    removeItem: function (k) { delete store[k]; },
  },
  addEventListener: noop, removeEventListener: noop,
};
const ctx = {
  window: window, console: console, setTimeout: setTimeout, clearTimeout: clearTimeout,
  setInterval: setInterval, clearInterval: clearInterval, URLSearchParams: URLSearchParams,
  WebSocket: function () {},
};
vm.runInNewContext(fs.readFileSync(jsPath, "utf8"), ctx, { filename: jsPath });
if (!page) throw new Error("kanban page did not register");
function find(node, pred) {
  if (!node || typeof node !== "object") return null;
  if (Array.isArray(node)) {
    for (const n of node) { const hit = find(n, pred); if (hit) return hit; }
    return null;
  }
  if (pred(node)) return node;
  return find(node.props && node.props.children, pred);
}
function text(node) {
  if (node == null || node === false || node === true) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  return text(node.props && node.props.children);
}
function leaves(node, out) {
  if (!node || typeof node !== "object") return out;
  if (Array.isArray(node)) { node.forEach(function (n) { leaves(n, out); }); return out; }
  const kids = [].concat(node.props && node.props.children || []).flat(Infinity);
  if (kids.every(function (k) { return k == null || typeof k !== "object"; })) out.push(text(node));
  else leaves(kids, out);
  return out;
}
const named = function (name) {
  return function (n) { return typeof n.type === "function" && n.type.name === name; };
};
const settle = function () { return new Promise(function (r) { setImmediate(r); }); };
(async function () {
  let strip = null, toolbar = null;
  for (let pass = 0; pass < 25 && !(strip && toolbar); pass++) {
    idx = 0; effects = [];
    const tree = page();
    effects.forEach(function (fn) { fn(); });
    for (let k = 0; k < 5; k++) await settle();
    strip = find(tree, named("BoardCapsStrip"));
    toolbar = find(tree, named("BoardToolbar"));
  }
  if (!toolbar) throw new Error("the board never loaded (BoardToolbar not rendered)");
  if (!strip) throw new Error("the page does not render BoardCapsStrip");
  const out = strip.type(strip.props);
  process.stdout.write(JSON.stringify({
    props_caps: strip.props.caps === undefined ? "undefined" : strip.props.caps,
    rendered: out != null && out !== false,
    text: text(out),
    leaves: leaves(out, []).filter(function (s) { return s !== ""; }),
  }));
  process.exit(0);
})().catch(function (e) { process.stderr.write(String(e && e.stack || e)); process.exit(3); });
"""


def _render_strip(tmp_path: Path, caps, *, bundle: Path = _BUNDLE, mode: str = "present") -> dict:
    """Run ``bundle`` with a /board response carrying ``caps`` (``mode``
    ``absent`` omits the key, as a backend predating caps does) and return the
    BoardCapsStrip the page renders."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the dashboard bundle")
    harness = tmp_path / "caps_strip_harness.js"
    harness.write_text(_STRIP_HARNESS, encoding="utf-8")
    spec = tmp_path / f"caps_spec_{abs(hash((mode, json.dumps(caps, sort_keys=True))))}.json"
    spec.write_text(json.dumps({"mode": mode, "caps": caps}), encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(bundle), str(spec)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _board_caps_via_api(root: Path, tmp_path: Path, monkeypatch, user_cfg: str, managed_cfg: str = "") -> dict:
    """The REAL GET /board caps object for a config (one resolver call)."""
    _write_cfg(root / "config.yaml", user_cfg)
    if managed_cfg:
        managed = tmp_path / "managed"
        _write_cfg(managed / "config.yaml", managed_cfg)
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
    r = TestClient(app).get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    return r.json()["caps"]


_ZERO_USER_CFG = (
    "kanban:\n  max_in_progress: 0\n  max_in_progress_by_assignee:\n    evo: 0\n"
    "  assignee_groups:\n    gpu:\n      max: 0\n      members: [evo, kimi]\n"
)
_ZERO_MANAGED_CFG = "kanban:\n  max_spawn: 0\n"
_WARNING_ONLY_CFG = "kanban:\n  max_spawn: \"4\"\n"


def _assert_strip_shows_zero_caps(result: dict) -> None:
    assert result["rendered"], f"BoardCapsStrip rendered nothing for configured caps: {result}"
    text = result["text"]
    for entry in ("max_spawn=0 (managed)", "max_in_progress=0 (user)",
                  "max_in_progress_by_assignee.evo=0 (user)",
                  "assignee_groups.gpu=max 0 [evo,kimi] (user)"):
        assert entry in result["leaves"], f"{entry!r} not rendered: {result['leaves']}"
    assert "caps:" in text, text
    assert "uncapped" not in text.lower(), f"a zero cap was shown as uncapped: {text!r}"


def _assert_strip_shows_warning_only(result: dict) -> None:
    assert result["rendered"], f"BoardCapsStrip hid a warning-only payload: {result}"
    warnings = [leaf for leaf in result["leaves"] if leaf.startswith("⚠ ")]
    assert warnings, f"no warning rendered: {result['leaves']}"
    assert any("max_spawn" in w and "ignoring invalid board caps" in w for w in warnings), warnings
    assert "caps:" not in result["text"], result["text"]  # no values to label


def test_dashboard_bundle_renders_zero_caps_with_scope(sticky_profile_home, tmp_path, monkeypatch):
    caps = _board_caps_via_api(sticky_profile_home, tmp_path, monkeypatch, _ZERO_USER_CFG, _ZERO_MANAGED_CFG)
    assert caps["values"]["max_spawn"] == 0 and caps["scope"]["max_spawn"] == "managed", caps
    result = _render_strip(tmp_path, caps)
    assert result["props_caps"] == caps  # the page hands the strip the /board caps
    _assert_strip_shows_zero_caps(result)


def test_dashboard_bundle_renders_warning_only_caps(sticky_profile_home, tmp_path, monkeypatch):
    caps = _board_caps_via_api(sticky_profile_home, tmp_path, monkeypatch, _WARNING_ONLY_CFG)
    assert caps["values"] == {} and caps["warnings"], caps
    _assert_strip_shows_warning_only(_render_strip(tmp_path, caps))


@pytest.mark.parametrize("mode,caps", [
    ("absent", None),
    ("present", None),
    ("present", {"values": {}, "scope": {}, "warnings": []}),
], ids=["backend-predates-caps", "null-caps", "nothing-configured"])
def test_dashboard_bundle_hides_strip_without_caps(tmp_path, mode, caps):
    result = _render_strip(tmp_path, caps, mode=mode)
    assert result["rendered"] is False, result


# Known-red controls: each plant edits ONLY BoardCapsStrip in a copy of the
# shipped bundle; the anchor must exist exactly once so a plant never no-ops.
_STRIP_PLANTS = {
    "returns-null": ("function BoardCapsStrip(props) {", "function BoardCapsStrip(props) { return null;"),
    "hides-warnings": ("const warnings = (caps.warnings || []).map(", "const warnings = [].map("),
    "zero-uncapped": (
        '        : String(value);\n      const isCapZero',
        '        : (Number(value) === 0 ? "uncapped" : String(value));\n      const isCapZero',
    ),
    "zero-blank": (
        '        : String(value);\n      const isCapZero',
        '        : (value ? String(value) : "");\n      const isCapZero',
    ),
    "drops-scope": ('key + "=" + shown + " (" + scope + ")"', 'key + "=" + shown'),
}


def _planted_bundle(tmp_path: Path, plant: str) -> Path:
    old, new = _STRIP_PLANTS[plant]
    src = _BUNDLE.read_text(encoding="utf-8")
    start = src.index("function BoardCapsStrip(props) {")
    end = src.index("// Toolbar", start)
    body = src[start:end]
    assert body.count(old) == 1, f"plant {plant!r} anchor not found exactly once in BoardCapsStrip"
    out = tmp_path / f"index.{plant}.js"
    out.write_text(src[:start] + body.replace(old, new) + src[end:], encoding="utf-8")
    return out


@pytest.mark.parametrize("plant,case", [
    ("returns-null", "zero"),
    ("returns-null", "warning-only"),
    ("hides-warnings", "warning-only"),
    ("zero-uncapped", "zero"),
    ("zero-blank", "zero"),
    ("drops-scope", "zero"),
])
def test_dashboard_bundle_controls_fail_on_planted_strip(sticky_profile_home, tmp_path, monkeypatch, plant, case):
    if case == "zero":
        caps = _board_caps_via_api(sticky_profile_home, tmp_path, monkeypatch, _ZERO_USER_CFG, _ZERO_MANAGED_CFG)
        check = _assert_strip_shows_zero_caps
    else:
        caps = _board_caps_via_api(sticky_profile_home, tmp_path, monkeypatch, _WARNING_ONLY_CFG)
        check = _assert_strip_shows_warning_only
    check(_render_strip(tmp_path, caps))  # the shipped strip passes ...
    with pytest.raises(AssertionError):  # ... and the planted one fails the SAME check
        check(_render_strip(tmp_path, caps, bundle=_planted_bundle(tmp_path, plant)))
