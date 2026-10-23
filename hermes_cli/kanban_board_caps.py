"""Board-scope kanban dispatch caps (#124).

A kanban board lives under the Hermes root and is shared by every profile on
the host, so its capacity caps must not depend on which profile runs a
dispatcher: the CLI ``dispatch`` verb, the standalone daemon, the gateway's
embedded dispatcher or the dashboard's dispatch nudge. Every one of them
resolves the caps through :func:`read_board_caps`, fresh on every call (loops
call it every tick), by one declared rule:

1. The board caps are the six :data:`BOARD_CAP_KEYS` under ``kanban:`` in the
   DEFAULT profile's ``config.yaml``, read as raw YAML, with the managed-scope
   overlay applied on top (managed wins at the leaf).
2. Board-cap values are literal YAML values. A ``${VAR}`` reference in a
   board-cap key is NOT expanded: that key is invalid, a warning names it,
   and it is ignored. The caps never depend on the invoker's environment or
   secret scope.
3. A missing default ``config.yaml`` is an empty user layer; the managed
   overlay still applies.
4. An unreadable or unparseable default ``config.yaml`` refuses spawning for
   that resolution (``max_spawn=0``; reclaim and promotion still run), with
   the reason in the warning. Nothing is cached and no backup is consulted;
   the next tick reads the file again. The invoking profile's caps are never
   a fallback.
5. ``max_spawn``, ``max_in_progress`` and ``max_in_progress_per_profile`` share
   one parser: a nonnegative integer, ``0`` kept as ``0`` (spawn nothing).

Everything else under ``kanban:`` (intervals, failure limits, alerts, worker
containment) is runtime config of the process running the dispatcher and keeps
resolving from that process's profile.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, Optional

# The board-scope capacity keys (issue #124): whose cards may be spawned, and
# how many workers may run at once.
BOARD_CAP_KEYS = (
    "default_assignee",
    "max_in_progress",
    "max_in_progress_per_profile",
    "max_in_progress_by_assignee",
    "assignee_groups",
    "max_spawn",
)
_INT_KEYS = ("max_spawn", "max_in_progress", "max_in_progress_per_profile")
_NAMED_KEYS = ("max_in_progress_by_assignee", "assignee_groups")

SOURCE_DEFAULT = "default profile config"
SOURCE_MANAGED = "managed scope"
SOURCE_CLI_MAX = "--max"
SOURCE_CLI_ASSIGNEE = "--max-per-assignee"
SOURCE_DERIVED = "memory-derived default"
SOURCE_REFUSED = "spawning refused"

_ENV_REF = re.compile(r"\$\{[^}]+\}")


class _Unreadable(Exception):
    """The default profile's config.yaml cannot be turned into board caps."""


@dataclass(frozen=True)
class BoardCaps:
    """One resolution of the board-scope caps.

    ``section`` holds the valid :data:`BOARD_CAP_KEYS` values (default file
    with the managed overlay applied); ``managed`` names the values the
    managed scope set (``key`` or ``key.name``). ``warning`` is one line or
    ``None``. ``spawn_refused`` is True when the default config is unreadable.
    """

    section: Dict[str, Any]
    source: str = SOURCE_DEFAULT
    warning: Optional[str] = None
    spawn_refused: bool = False
    managed: FrozenSet[str] = frozenset()


@dataclass
class DispatchCaps:
    """Parsed caps ready for ``dispatch_once(**kwargs)``, with per-value sources."""

    kwargs: Dict[str, Any]
    sources: Dict[str, str] = field(default_factory=dict)


def default_profile_config_path() -> Path:
    """``<default home>/config.yaml`` — the board-scope cap source."""
    from hermes_cli.profiles import _get_default_hermes_home
    return _get_default_hermes_home() / "config.yaml"


def _cap_keys(doc: Any, what: str) -> Dict[str, Any]:
    """The board-cap keys of a parsed YAML document's ``kanban:`` section."""
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise _Unreadable(f"{what}: top-level YAML is not a mapping")
    kanban = doc.get("kanban")
    if kanban is None:
        return {}
    if not isinstance(kanban, dict):
        raise _Unreadable(f"{what}: kanban: is not a mapping")
    return {k: kanban[k] for k in BOARD_CAP_KEYS if k in kanban}


def _read_default_caps(path: Path) -> Optional[Dict[str, Any]]:
    """ONE read of the default config's raw board-cap keys; ``None`` when the
    file does not exist; raises :class:`_Unreadable` on any other failure."""
    from hermes_cli import config as cfgmod

    try:
        with cfgmod._CONFIG_LOCK:  # libyaml is not thread-safe on one file
            with open(path, encoding="utf-8") as f:
                doc = cfgmod.fast_safe_load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise _Unreadable(type(exc).__name__) from exc
    return _cap_keys(doc, "config.yaml")


def _managed_caps() -> Dict[str, Any]:
    """The managed scope's raw board-cap keys (``{}`` when no scope is set;
    the managed loader is fail-open and logs its own parse errors)."""
    from hermes_cli import managed_scope

    try:
        return _cap_keys(managed_scope.load_managed_config(), "managed config")
    except Exception:
        return {}


def read_board_caps() -> BoardCaps:
    """Resolve the board-scope caps now (see the module docstring). Never raises."""
    from hermes_cli.config import _deep_merge

    path: Any = "default profile config.yaml"
    try:
        path = default_profile_config_path()
        user = _read_default_caps(path)
    except Exception as exc:  # rule 4: fail closed, never the invoker's caps
        reason = str(exc) if isinstance(exc, _Unreadable) else type(exc).__name__
        return BoardCaps(
            {}, f"{SOURCE_REFUSED}; {path} unreadable",
            warning=(
                f"kanban: default profile config ({path}) unreadable ({reason}); "
                f"refusing to spawn workers until it is fixed"
            ),
            spawn_refused=True,
        )
    source = f"{SOURCE_DEFAULT} ({path})"
    if user is None:  # rule 3: no default config is an empty user layer
        user, source = {}, f"{SOURCE_DEFAULT} ({path} missing: no user caps)"
    managed = _managed_caps()
    section = _deep_merge(user, managed)
    managed_keys = set()
    for key, value in managed.items():
        if value is None:  # _deep_merge keeps the user value under a managed None
            continue
        managed_keys.add(key)
        if key in _NAMED_KEYS and isinstance(value, dict):
            managed_keys.update(f"{key}.{name}" for name in value)
    # rule 2: a ${VAR} anywhere in a key's value (nested too) invalidates that key
    ignored = sorted(k for k in section if _ENV_REF.search(repr(section[k])))
    for key in ignored:
        del section[key]
    warnings = []
    if ignored:
        warnings.append(
            "kanban: board caps take literal values only; ignoring "
            + ", ".join(ignored)
            + " (a ${VAR} reference is not expanded)"
        )
    mismatch = _mismatch_line(section, path)
    if mismatch:
        warnings.append(mismatch)
    return BoardCaps(
        section, source,
        warning="; ".join(warnings) or None,
        managed=frozenset(managed_keys),
    )


def parse_caps(section: Dict[str, Any]) -> Dict[str, Any]:
    """The six caps as configured: ``None`` / empty when unset or invalid.

    The integer caps go through ONE parser (``_strict_int(minimum=0)``): a
    nonnegative integer, ``0`` kept as ``0``. The named maps use the
    ``kanban_db_dispatch`` parsers every dispatcher already used, so a typo
    drops one entry instead of bricking dispatch. Never raises.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    section = section if isinstance(section, dict) else {}
    raw_default = section.get("default_assignee")
    default_assignee = raw_default.strip() if isinstance(raw_default, str) else ""
    parsed: Dict[str, Any] = {
        key: kbd._strict_int(section.get(key), minimum=0) for key in _INT_KEYS
    }
    parsed["default_assignee"] = default_assignee or None
    parsed["max_in_progress_by_assignee"] = kbd.parse_assignee_caps(
        section.get("max_in_progress_by_assignee")
    )
    parsed["assignee_groups"] = kbd.parse_assignee_groups(section.get("assignee_groups"))
    return parsed


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _mismatch_line(section: Dict[str, Any], default_path: Path) -> Optional[str]:
    """One line naming every board cap whose PARSED value differs between the
    board caps and the invoking profile's own config, or ``None`` when they
    agree or the invoker is the default profile. Informational only."""
    try:
        from hermes_cli.config import get_config_path, load_config_readonly

        if _same_path(get_config_path(), default_path):
            return None
        invoking = (load_config_readonly() or {}).get("kanban")
        invoking = invoking if isinstance(invoking, dict) else {}
    except Exception:
        return None
    ours, theirs = parse_caps(section), parse_caps(invoking)
    diffs = [
        f"{key} default={ours[key]!r} invoking={theirs[key]!r}"
        for key in BOARD_CAP_KEYS
        if ours[key] != theirs[key]
    ]
    if not diffs:
        return None
    return (
        "kanban: invoking profile caps differ from default profile caps ("
        + "; ".join(diffs)
        + f"); board caps taken from the default profile config ({default_path})"
    )


def dispatch_caps(
    caps: BoardCaps,
    *,
    cli_max: Optional[int] = None,
    cli_assignee_caps: Optional[Iterable[str] | Dict[str, int]] = None,
    kbd: Any = None,
) -> DispatchCaps:
    """``dispatch_once`` kwargs for ``caps`` plus explicit operator overrides.

    Explicit ``--max`` wins over ``kanban.max_spawn``; explicit
    ``--max-per-assignee`` pairs win per name. A refused resolution forces
    ``max_spawn=0`` (reclaim and promotion still run; nothing new spawns),
    which no override lifts. ``kbd`` is the ``kanban_db_dispatch`` module to
    resolve the memory-derived default with; callers inside that module pass
    their own so a re-imported copy never splits the two.
    """
    if kbd is None:
        from hermes_cli import kanban_db_dispatch as kbd

    parsed = parse_caps(caps.section)

    def origin(key: str) -> str:
        return SOURCE_MANAGED if key in caps.managed else SOURCE_DEFAULT

    sources: Dict[str, str] = {}
    for key in ("default_assignee", "max_in_progress_per_profile", "max_spawn", "max_in_progress"):
        if parsed[key] is not None:
            sources[key] = origin(key)
    for key in _NAMED_KEYS:
        for name in parsed[key]:
            sources[f"{key}.{name}"] = origin(f"{key}.{name}")

    max_in_progress = kbd.resolve_max_in_progress(parsed["max_in_progress"])
    if parsed["max_in_progress"] is None and max_in_progress is not None:
        sources["max_in_progress"] = SOURCE_DERIVED

    by_assignee = dict(parsed["max_in_progress_by_assignee"])
    for name, cap in kbd.parse_assignee_caps(cli_assignee_caps).items():
        by_assignee[name] = cap
        sources[f"max_in_progress_by_assignee.{name}"] = SOURCE_CLI_ASSIGNEE

    max_spawn = parsed["max_spawn"]
    if cli_max is not None:
        max_spawn = cli_max
        sources["max_spawn"] = SOURCE_CLI_MAX
    if caps.spawn_refused:
        max_spawn = 0
        sources["max_spawn"] = SOURCE_REFUSED

    return DispatchCaps(
        kwargs={
            "default_assignee": parsed["default_assignee"],
            "max_in_progress": max_in_progress,
            "max_in_progress_per_profile": parsed["max_in_progress_per_profile"],
            "max_in_progress_by_assignee": by_assignee,
            "assignee_groups": parsed["assignee_groups"],
            "max_spawn": max_spawn,
        },
        sources=sources,
    )


def cap_rows(resolved: DispatchCaps) -> list:
    """``[(key, value, source), ...]`` for every cap in effect."""
    kw = resolved.kwargs
    src = resolved.sources
    rows = []
    for key in ("max_spawn", "max_in_progress", "max_in_progress_per_profile", "default_assignee"):
        if kw.get(key) is not None:
            rows.append((key, kw[key], src.get(key, SOURCE_DEFAULT)))
    for key in _NAMED_KEYS:
        for name, value in sorted(kw.get(key, {}).items()):
            k = f"{key}.{name}"
            rows.append((k, dict(value) if isinstance(value, dict) else value, src.get(k, SOURCE_DEFAULT)))
    return rows


def describe_caps(resolved: DispatchCaps) -> list:
    """Human lines (``  key: value (source)``) for every cap in effect."""
    lines = []
    for key, value, source in cap_rows(resolved):
        if isinstance(value, dict):
            value = f"max {value.get('max')} [{','.join(value.get('members', []))}]"
        lines.append(f"  {key}: {value} ({source})")
    return lines


class WarnOnChange:
    """Emit a caps warning only when its text changes (loops tick every few
    seconds; an unchanged warning must not flood the log)."""

    def __init__(self, emit) -> None:
        self._emit = emit
        self._last: Optional[str] = None

    def __call__(self, warning: Optional[str]) -> None:
        if warning and warning != self._last:
            self._emit(warning)
        self._last = warning
