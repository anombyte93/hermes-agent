"""Board-scope kanban dispatch caps (#124).

A kanban board lives under the Hermes root and is shared by every profile on
the host, so its capacity caps must not depend on which profile runs a
dispatcher: the CLI ``dispatch`` verb, the standalone daemon, the gateway's
embedded dispatcher or the dashboard's dispatch nudge. Every one of them
resolves the caps through :func:`read_board_caps`, fresh on every call (loops
call it every tick), by one declared rule:

1. The board caps are the six :data:`BOARD_CAP_KEYS` under ``kanban:`` in the
   DEFAULT profile's ``config.yaml``, read as raw YAML, with the managed-scope
   overlay applied on top. Managed wins per entry: a scalar, one
   ``max_in_progress_by_assignee`` name, or one field of one
   ``assignee_groups`` group.
2. Board caps are literal values and nothing here expands a ``${...}``
   reference. Integer caps are literal YAML integers >= 0 (``max_spawn``,
   ``max_in_progress``, ``max_in_progress_per_profile``, each
   ``max_in_progress_by_assignee`` entry and each group ``max``);
   ``default_assignee`` and group members are literal names. Each entry or group is validated ON ITS OWN, in its own layer: an
   invalid one (a reference, a string or bool where an int belongs, ``null``,
   ``{}``) is dropped and named in the warning, and every valid entry, managed
   or user, is kept.
3. A missing default ``config.yaml`` is an empty user layer; the managed
   overlay still applies.
4. An unreadable or unparseable default ``config.yaml`` refuses spawning for
   that resolution (``max_spawn=0``; reclaim and promotion still run), with
   the reason in the warning. Nothing is cached and no backup is consulted;
   the next tick reads the file again. The invoking profile's caps are never
   a fallback.
5. ``0`` stays ``0`` (spawn nothing / run nothing), for the scalar caps,
   one named assignee cap and one group's ``max`` alike; zero is never
   read as "uncapped".
6. Warnings name KEYS only, never a value read from any profile. The invoking
   profile's caps are read raw (never expanded) and validated by rule 2 only
   to name the keys that differ.
7. A value is labelled managed only when a valid managed value contributed
   to it; ``null`` / ``{}`` placeholders leave the user's value user-scoped.

Everything else under ``kanban:`` (intervals, failure limits, alerts, worker
containment) is runtime config of the process running the dispatcher and keeps
resolving from that process's profile.
"""
from __future__ import annotations

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


class _Unreadable(Exception):
    """The default profile's config.yaml cannot be turned into board caps."""


@dataclass(frozen=True)
class BoardCaps:
    """One resolution of the board-scope caps.

    ``section`` holds the valid :data:`BOARD_CAP_KEYS` values (default file
    with the managed overlay applied); ``managed`` names the values the
    managed scope contributed to (``key`` or ``key.name``). ``warning`` is one line or
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


def _read_raw_caps(path: Path) -> Optional[Dict[str, Any]]:
    """ONE raw read (no ``${...}`` expansion) of a config file's board-cap
    keys; ``None`` when the file does not exist; raises :class:`_Unreadable`
    on any other failure."""
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


def _literal_int(value: Any, minimum: int) -> Optional[int]:
    """``value`` when it is a literal YAML int >= ``minimum`` (never a bool or
    a string), else ``None``."""
    return value if type(value) is int and value >= minimum else None


def _literal_name(value: Any) -> Optional[str]:
    """A non-empty literal name (no ``${...}``), stripped, else ``None``."""
    if isinstance(value, str) and "${" not in value:
        return value.strip() or None
    return None


def _literal_group(spec: Any) -> Optional[Dict[str, Any]]:
    """One layer's valid fields of one group (``max`` and/or ``members``), or
    ``None`` when the group is invalid in that layer (a bad field, ``null``,
    ``{}``)."""
    if not isinstance(spec, dict) or not spec:
        return None
    out: Dict[str, Any] = {}
    if "max" in spec:
        out["max"] = _literal_int(spec["max"], 0)
        if out["max"] is None:
            return None
    if "members" in spec:
        raw = spec["members"]
        raw = raw.split(",") if isinstance(raw, str) else raw
        if not isinstance(raw, list):
            return None
        members: list = []
        for member in raw:
            name = _literal_name(member)
            if name is None:
                return None
            if name not in members:
                members.append(name)
        if not members:
            return None
        out["members"] = members
    return out or None


def _valid_layer(raw: Dict[str, Any]) -> tuple:
    """``(valid, invalid_keys)`` for one layer's raw board-cap keys (rule 2):
    each scalar, assignee entry and group is checked on its own."""
    valid: Dict[str, Any] = {}
    invalid: list = []
    for key, value in raw.items():
        if key in _NAMED_KEYS:
            if not isinstance(value, dict) or not value:
                invalid.append(key)
                continue
            entries: Dict[str, Any] = {}
            for name, entry in value.items():
                clean_name = _literal_name(name)
                if key == "assignee_groups":
                    clean = _literal_group(entry)
                else:
                    clean = _literal_int(entry, 0)
                if clean_name is None or clean is None:
                    invalid.append(f"{key}.{name}")
                else:
                    entries[clean_name] = clean
            if entries:
                valid[key] = entries
            continue
        clean = _literal_name(value) if key == "default_assignee" else _literal_int(value, 0)
        if clean is None:
            invalid.append(key)
        else:
            valid[key] = clean
    return valid, invalid


def _merge(user: Dict[str, Any], managed: Dict[str, Any]) -> tuple:
    """``(section, managed_keys, incomplete_groups)``: two VALID layers merged
    per entry (managed wins; a group merges per field). ``managed_keys`` names
    exactly what a managed value contributed to (rule 7). A group left
    without both ``members`` and ``max`` is dropped and named."""
    section = {k: dict(v) if isinstance(v, dict) else v for k, v in user.items()}
    managed_keys = set()
    for key, value in managed.items():
        if key == "assignee_groups":
            groups = section.setdefault(key, {})
            for name, spec in value.items():
                groups[name] = {**groups.get(name, {}), **spec}
                managed_keys.add(f"{key}.{name}")
        elif key == "max_in_progress_by_assignee":
            section.setdefault(key, {}).update(value)
            managed_keys.update(f"{key}.{name}" for name in value)
        else:
            section[key] = value
            managed_keys.add(key)
    groups = section.get("assignee_groups", {})
    incomplete = [f"assignee_groups.{name}" for name in groups
                  if not {"members", "max"} <= set(groups[name])]
    for key in incomplete:
        del groups[key.split(".", 1)[1]]
    return section, managed_keys, incomplete


def read_board_caps() -> BoardCaps:
    """Resolve the board-scope caps now (see the module docstring). Never raises."""
    path: Any = "default profile config.yaml"
    try:
        path = default_profile_config_path()
        user = _read_raw_caps(path)
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
    user_valid, user_invalid = _valid_layer(user)
    managed_valid, managed_invalid = _valid_layer(_managed_caps())
    section, managed_keys, incomplete = _merge(user_valid, managed_valid)
    invalid = sorted(set(user_invalid + managed_invalid + incomplete))
    warnings = []
    if invalid:  # rule 6: key names only
        warnings.append(
            "kanban: ignoring invalid board caps (literal integers and names "
            "only; a ${...} reference is never expanded): " + ", ".join(invalid)
        )
    mismatch = _mismatch_line(section, managed_valid, path)
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


def _mismatch_line(
    section: Dict[str, Any], managed: Dict[str, Any], default_path: Path,
) -> Optional[str]:
    """One line naming (KEYS only, rule 6) every board cap that differs from
    the invoking profile's own raw caps under the same managed overlay, or
    ``None`` when they agree or the invoker is the default profile.
    Informational only."""
    try:
        from hermes_cli.config import get_config_path

        invoking_path = get_config_path()
        if _same_path(invoking_path, default_path):
            return None
        raw = _read_raw_caps(invoking_path) or {}
    except Exception:
        return None
    theirs = _merge(_valid_layer(raw)[0], managed)[0]

    def value(caps: Dict[str, Any], key: str) -> Any:  # an empty map is unset
        return (caps.get(key) or None) if key in _NAMED_KEYS else caps.get(key)

    diffs = [key for key in BOARD_CAP_KEYS if value(section, key) != value(theirs, key)]
    if not diffs:
        return None
    return (
        "kanban: invoking profile caps differ from default profile caps ("
        + ", ".join(diffs)
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


def caps_display(caps: BoardCaps) -> Dict[str, Any]:
    """The resolved board caps as one JSON-safe object for surfaces that SHOW
    them (``kanban list`` header, dashboard): ``values`` (each cap in effect),
    ``scope`` (``managed``/``user`` per value; a group's members ride its key)
    and ``warnings`` (the resolver's key-only warning lines, split on ``;``).

    ONE resolution, ONE shape: every display surface renders this object and
    never re-implements resolution. Derived-from-memory values
    (``SOURCE_DERIVED``) report the scope they were derived from as ``user``;
    a display shows what a cap IS, not how the memory heuristic picked it.
    """
    resolved = dispatch_caps(caps)
    values: Dict[str, Any] = {}
    scope: Dict[str, str] = {}
    for key, value, source in cap_rows(resolved):
        values[key] = value
        scope[key] = "managed" if source == SOURCE_MANAGED else "user"
    warnings = [w.strip() for w in (caps.warning or "").split(";") if w.strip()]
    return {"values": values, "scope": scope, "warnings": warnings}


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
