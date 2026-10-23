"""Board-scope kanban dispatch caps (#124).

A kanban board lives under the Hermes root and is shared by every profile on
the host, so its concurrency caps must hold whichever profile runs
``dispatch`` — a CLI, the gateway daemon, a script or a worker. The
board-scope source is the DEFAULT profile's ``config.yaml``; when that file
cannot be read (missing, unreadable or unparseable), callers fall back to the
invoking profile's config — the pre-fix behaviour — and warn, so dispatch
never crashes on a broken config.

Only the six capacity keys below are board-scope. Everything else under
``kanban:`` (intervals, alerts, worker containment) is runtime config of the
process running the dispatcher, not board capacity, and keeps resolving from
the invoking profile.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# The board-scope capacity keys (issue #124): whose cards may be spawned, and
# how many workers may run at once. ``max_in_progress_by_assignee`` and
# ``assignee_groups`` are mappings; the rest are scalars or strings. Each key's
# schema default equals its unset value, so raw and merged configs agree on
# presence for these keys and the ``kanban_db_dispatch`` parsers accept either.
BOARD_CAP_KEYS = (
    "default_assignee",
    "max_in_progress",
    "max_in_progress_per_profile",
    "max_in_progress_by_assignee",
    "assignee_groups",
    "max_spawn",
)


def default_profile_config_path():
    """``<default home>/config.yaml`` — the board-scope cap source."""
    from hermes_cli.profiles import _get_default_hermes_home
    return _get_default_hermes_home() / "config.yaml"


def _read_default_profile_section() -> Tuple[Optional[Dict[str, Any]], str]:
    """``(kanban: section of the default profile's config, path)``.

    ``None`` means the file cannot be read (missing, unreadable or unparseable)
    — the caller falls back to the invoking profile's config and warns. A
    readable file with no ``kanban:`` section yields ``{}``: readable, no
    board caps set. The section is read and env-expanded the way the other
    board-scope reader (:func:`hermes_cli.config.read_root_config_section`)
    does, since it resolves the very same file. Reads ONLY the default
    profile's config; no other profile directory is enumerated or opened.
    """
    path = default_profile_config_path()
    try:
        if not path.is_file():
            return None, str(path)
        # Probe parseability first: read_root_config_section collapses
        # "unparseable" into {}, which must instead fall back with a warning.
        from hermes_cli.config import read_user_config_raw
        read_user_config_raw(path)
    except Exception:
        return None, str(path)
    try:
        from hermes_cli.config import read_root_config_section
        section = read_root_config_section("kanban")
    except Exception:
        return {}, str(path)
    return (section if isinstance(section, dict) else {}), str(path)


def _invoking_raw_section() -> Dict[str, Any]:
    """The invoking profile's RAW ``kanban:`` section, for mismatch warnings.

    Presence is judged on the raw file (the merged config always carries the
    schema defaults, which would hide a real difference); values are compared
    as written.
    """
    try:
        from hermes_cli.config import read_raw_config_readonly
        section = (read_raw_config_readonly() or {}).get("kanban")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _invoking_merged_section() -> Tuple[Dict[str, Any], str]:
    """``(invoking profile's merged kanban: section, config path)`` — the
    pre-fix cap source, used only as the unreadable-default fallback."""
    try:
        from hermes_cli.config import get_config_path, load_config
        section = (load_config() or {}).get("kanban")
        return (section if isinstance(section, dict) else {}), str(get_config_path())
    except Exception:
        return {}, ""


def _mismatch_line(
    default_section: Dict[str, Any], invoking_section: Dict[str, Any], default_path: str,
) -> Optional[str]:
    """One line naming every board cap whose value differs between the two
    configs, or ``None`` when they agree (including when neither sets any)."""
    diffs = []
    for key in BOARD_CAP_KEYS:
        in_default = key in default_section
        in_invoking = key in invoking_section
        if not (in_default or in_invoking):
            continue
        if in_default and in_invoking and default_section[key] == invoking_section[key]:
            continue
        v_default = default_section[key] if in_default else "unset"
        v_invoking = invoking_section[key] if in_invoking else "unset"
        diffs.append(f"{key} default={v_default!r} invoking={v_invoking!r}")
    if not diffs:
        return None
    return (
        "kanban: invoking profile caps differ from default profile caps ("
        + "; ".join(diffs)
        + f"); board caps taken from the default profile config ({default_path})"
    )


def board_scope_kanban_caps() -> Tuple[Dict[str, Any], str, Optional[str]]:
    """``(kanban cfg, source label, warning line or None)`` for dispatch caps.

    The cfg carries the six :data:`BOARD_CAP_KEYS`; callers parse them with
    the ``kanban_db_dispatch`` helpers exactly as they parsed
    ``load_config()`` values. The source label names the config the caps came
    from (path included) for the dry-run summary. The warning is a single
    line when the invoking profile's raw caps differ from the default
    profile's, or when the default profile's config could not be read and the
    invoking profile's config is being used instead.
    """
    default_section, default_path = _read_default_profile_section()
    if default_section is None:
        invoking, invoking_path = _invoking_merged_section()
        return invoking, f"invoking profile config ({invoking_path})", (
            f"default profile config ({default_path}) unreadable or missing; "
            f"falling back to the invoking profile's kanban caps ({invoking_path})"
        )
    warning = _mismatch_line(default_section, _invoking_raw_section(), default_path)
    return default_section, f"default profile config ({default_path})", warning
