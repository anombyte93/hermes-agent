"""Board-scope kanban dispatch caps (#124).

A kanban board lives under the Hermes root and is shared by every profile on
the host, so its concurrency caps must hold whichever profile runs a
dispatcher — the CLI ``dispatch`` verb, the standalone daemon, the gateway's
embedded dispatcher or the dashboard's dispatch nudge. The board-scope source
is the DEFAULT profile's ``config.yaml``, resolved through the same effective
pipeline ``load_config()`` applies (schema defaults, ``${VAR}`` expansion and
the managed-scope overlay), from ONE read of the file per resolution.

Loops call :func:`read_board_caps` every tick, so an edit to the default
profile's config applies on the next tick. When that file cannot be read or
parsed, the caps fail safe, in order:

1. the last good caps this process resolved from the file;
2. the default profile's last-known-good backup (``backups/config/``, the copy
   the normal config loader keeps of the last file that parsed);
3. otherwise no known-good caps exist and spawning is refused
   (:attr:`BoardCaps.spawn_refused`) until the file is fixed.

The invoking profile's caps are never used as a fallback: that is the
profile-dependent rule #124 removes.

Only the six capacity keys below are board-scope. Everything else under
``kanban:`` (intervals, failure limits, alerts, worker containment) is runtime
config of the process running the dispatcher and keeps resolving from the
invoking profile.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

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

SOURCE_DEFAULT = "default profile config"
SOURCE_LAST_GOOD = "last good default profile config (kept in-process)"
SOURCE_BACKUP = "default profile config last-known-good backup"
SOURCE_NONE = "no known-good board caps"
SOURCE_CLI_MAX = "--max"
SOURCE_CLI_ASSIGNEE = "--max-per-assignee"
SOURCE_DERIVED = "memory-derived default"
SOURCE_REFUSED = "spawning refused"

# default config path -> the last good board-cap section resolved from it.
_LAST_GOOD: Dict[str, Dict[str, Any]] = {}
_LAST_GOOD_LOCK = threading.Lock()


class _DefaultConfigUnreadable(Exception):
    """The default profile's config.yaml could not be turned into board caps."""


@dataclass(frozen=True)
class BoardCaps:
    """One resolution of the board-scope caps.

    ``section`` holds the six :data:`BOARD_CAP_KEYS` as the effective config
    carries them; ``origin`` names which known-good layer they came from
    (one of the ``SOURCE_*`` labels) and ``source`` adds the path for humans.
    ``warning`` is a single line or ``None``. ``spawn_refused`` is True only
    when no known-good caps exist at all.
    """

    section: Dict[str, Any]
    origin: str
    source: str
    warning: Optional[str] = None
    spawn_refused: bool = False
    path: str = ""


@dataclass
class DispatchCaps:
    """Parsed caps ready for ``dispatch_once(**kwargs)``, with per-value sources."""

    kwargs: Dict[str, Any]
    sources: Dict[str, str] = field(default_factory=dict)


def default_profile_config_path() -> Path:
    """``<default home>/config.yaml`` — the board-scope cap source."""
    from hermes_cli.profiles import _get_default_hermes_home
    return _get_default_hermes_home() / "config.yaml"


def _cap_section(effective: Any) -> Dict[str, Any]:
    """The six board-cap keys of an effective config; raises when ``kanban:``
    is not a mapping (a malformed section is unreadable, not "no caps")."""
    if not isinstance(effective, dict):
        raise _DefaultConfigUnreadable("top-level config is not a mapping")
    kanban = effective.get("kanban")
    if kanban is None:
        return {}
    if not isinstance(kanban, dict):
        raise _DefaultConfigUnreadable("kanban: is not a mapping")
    return {k: kanban[k] for k in BOARD_CAP_KEYS if k in kanban}


def _read_effective_caps(path: Path) -> Optional[Dict[str, Any]]:
    """ONE read of ``path`` -> its effective board-cap section.

    ``None`` when the file does not exist. Raises on any other read, parse or
    shape error, so the caller can never mistake a failed read for an empty
    config.
    """
    from hermes_cli import config as cfgmod

    with cfgmod._CONFIG_LOCK:  # libyaml is not thread-safe on one file
        try:
            with open(path, encoding="utf-8") as f:
                raw = cfgmod.fast_safe_load(f)
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise _DefaultConfigUnreadable(type(exc).__name__) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _DefaultConfigUnreadable("top-level YAML is not a mapping")
    try:
        effective = cfgmod.effective_config_from_raw(raw)
    except Exception as exc:
        raise _DefaultConfigUnreadable(type(exc).__name__) from exc
    return _cap_section(effective)


def _backup_caps(path: Path) -> Optional[Tuple[Dict[str, Any], str]]:
    """``(caps, backups dir)`` from the default profile's newest good backup."""
    try:
        from hermes_cli import config as cfgmod
        from hermes_cli.config_backups import backups_dir, load_newest_good_backup

        raw = load_newest_good_backup(path)
        if raw is None:
            return None
        return _cap_section(cfgmod.effective_config_from_raw(raw)), str(backups_dir(path))
    except Exception:
        return None


def parse_caps(section: Dict[str, Any]) -> Dict[str, Any]:
    """The six caps as configured: ``None`` / empty when unset or invalid.

    Uses the ``kanban_db_dispatch`` parsers every dispatcher already used, so
    a typo drops one entry instead of bricking dispatch. Never raises.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    section = section if isinstance(section, dict) else {}
    raw_default = section.get("default_assignee")
    default_assignee = raw_default.strip() if isinstance(raw_default, str) else ""
    return {
        "default_assignee": default_assignee or None,
        "max_in_progress": kbd._positive_int(section.get("max_in_progress"), None),
        "max_in_progress_per_profile": kbd._positive_int(
            section.get("max_in_progress_per_profile"), None
        ),
        "max_in_progress_by_assignee": kbd.parse_assignee_caps(
            section.get("max_in_progress_by_assignee")
        ),
        "assignee_groups": kbd.parse_assignee_groups(section.get("assignee_groups")),
        "max_spawn": kbd._positive_int(section.get("max_spawn"), None),
    }


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _mismatch_line(section: Dict[str, Any], default_path: Path) -> Optional[str]:
    """One line naming every board cap whose PARSED value differs between the
    default profile's effective config and the invoking profile's effective
    config, or ``None`` when they agree or the invoker is the default profile.

    Both sides go through the same defaults + expansion + managed overlay and
    the same parsers, so equivalent spellings (``${VAR}`` vs its value,
    ``"2"`` vs ``2``) never produce a false mismatch.
    """
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


def read_board_caps() -> BoardCaps:
    """Resolve the board-scope caps now (one read of the default config).

    See the module docstring for the fail-safe order. Never raises.
    """
    try:
        path = default_profile_config_path()
    except Exception as exc:  # pragma: no cover - profile layer unavailable
        return BoardCaps(
            {}, SOURCE_NONE, f"{SOURCE_NONE} (default profile path unresolved)",
            warning=(
                f"kanban: default profile config path cannot be resolved "
                f"({type(exc).__name__}); no known-good board caps, refusing to spawn workers"
            ),
            spawn_refused=True,
        )
    key = str(path)
    try:
        section = _read_effective_caps(path)
        if section is None:
            with _LAST_GOOD_LOCK:
                had_good = key in _LAST_GOOD
            if had_good:
                raise _DefaultConfigUnreadable("file disappeared")
            # A host whose default profile never had a config: no caps set.
            section = {}
    except _DefaultConfigUnreadable as exc:
        return _recover(path, str(exc))
    with _LAST_GOOD_LOCK:
        _LAST_GOOD[key] = dict(section)
    return BoardCaps(
        section, SOURCE_DEFAULT, f"{SOURCE_DEFAULT} ({path})",
        warning=_mismatch_line(section, path), path=key,
    )


def _recover(path: Path, reason: str) -> BoardCaps:
    key = str(path)
    head = f"kanban: default profile config ({path}) unreadable ({reason})"
    with _LAST_GOOD_LOCK:
        last_good = _LAST_GOOD.get(key)
    if last_good is not None:
        return BoardCaps(
            dict(last_good), SOURCE_LAST_GOOD, f"{SOURCE_LAST_GOOD}; {path} unreadable",
            warning=f"{head}; keeping the last good board caps", path=key,
        )
    backup = _backup_caps(path)
    if backup is not None:
        section, where = backup
        return BoardCaps(
            section, SOURCE_BACKUP, f"{SOURCE_BACKUP} ({where}); {path} unreadable",
            warning=f"{head}; keeping the last good board caps from its last-known-good backup",
            path=key,
        )
    return BoardCaps(
        {}, SOURCE_NONE, f"{SOURCE_NONE}; {path} unreadable",
        warning=f"{head}; no last good board caps, refusing to spawn workers until it is fixed",
        spawn_refused=True, path=key,
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
    origin = caps.origin
    sources: Dict[str, str] = {}
    for key in ("default_assignee", "max_in_progress_per_profile", "max_spawn", "max_in_progress"):
        if parsed[key] is not None:
            sources[key] = origin
    for name in parsed["max_in_progress_by_assignee"]:
        sources[f"max_in_progress_by_assignee.{name}"] = origin
    for name in parsed["assignee_groups"]:
        sources[f"assignee_groups.{name}"] = origin

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
    for name, cap in sorted(kw.get("max_in_progress_by_assignee", {}).items()):
        k = f"max_in_progress_by_assignee.{name}"
        rows.append((k, cap, src.get(k, SOURCE_DEFAULT)))
    for name, spec in sorted(kw.get("assignee_groups", {}).items()):
        k = f"assignee_groups.{name}"
        rows.append((k, dict(spec), src.get(k, SOURCE_DEFAULT)))
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
