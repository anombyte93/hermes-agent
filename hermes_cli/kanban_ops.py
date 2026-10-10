"""Dispatcher and maintenance verbs for ``hermes kanban``: ``dispatch``,
``daemon`` (deprecated standalone loop), ``tail``/``watch`` event streaming,
``gc`` and ``repair``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_output import _err, _fmt_ts, _print_json


def _kanban_config() -> dict:
    """``config.yaml`` ``kanban:`` section, or ``{}`` when config can't be loaded."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return (cfg.get("kanban", {}) if isinstance(cfg, dict) else {}) or {}
    except Exception:
        return {}


def _poll_loop(interval: float, tick) -> int:
    """Run ``tick()`` every ``interval`` seconds (floor 0.1) until Ctrl-C."""
    try:
        while True:
            tick()
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")

    def tick():
        nonlocal last_id
        with kbc.connect_closing() as conn:
            events = kb.list_events(conn, args.task_id)
        for e in events:
            if e.id > last_id:
                pl = f" {e.payload}" if e.payload else ""
                print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                last_id = e.id

    return _poll_loop(args.interval, tick)


def _cmd_dispatch(args: argparse.Namespace) -> int:
    # Honour kanban.default_assignee, kanban.max_in_progress,
    # kanban.max_in_progress_per_profile and kanban.max_spawn with the same
    # semantics as the gateway dispatch path.
    try:
        from hermes_cli.config import load_config
        _cfg = load_config()
        _kanban_cfg = _cfg.get("kanban", {}) if isinstance(_cfg, dict) else {}
        default_assignee = (_kanban_cfg.get("default_assignee") or "").strip() or None
        max_in_progress_per_profile = kbd._positive_int(
            _kanban_cfg.get("max_in_progress_per_profile"), None
        )
        by_assignee = kbd.parse_assignee_caps(_kanban_cfg.get("max_in_progress_by_assignee"))
        assignee_groups = kbd.parse_assignee_groups(_kanban_cfg.get("assignee_groups"))
        # Memory-derived default when unset — same fallback the gateway applies.
        max_in_progress = kbd.resolve_max_in_progress(
            kbd._positive_int(_kanban_cfg.get("max_in_progress"), None)
        )
        # CLI --max is the more explicit signal, so it wins over kanban.max_spawn.
        cli_max = getattr(args, "max", None)
        max_spawn = (
            cli_max if cli_max is not None else kbd._positive_int(_kanban_cfg.get("max_spawn"), None)
        )
    except Exception:
        default_assignee = max_in_progress_per_profile = max_in_progress = None
        by_assignee = {}
        assignee_groups = {}
        max_spawn = getattr(args, "max", None)
    # Explicit --max-per-assignee pairs win per name over config (#83).
    by_assignee.update(kbd.parse_assignee_caps(getattr(args, "max_per_assignee", None)))
    with kbc.connect_closing() as conn:
        try:
            res = kbd.dispatch_once(
                conn,
                dry_run=args.dry_run,
                max_spawn=max_spawn,
                max_in_progress=max_in_progress,
                failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
                default_assignee=default_assignee,
                max_in_progress_per_profile=max_in_progress_per_profile,
                max_in_progress_by_assignee=by_assignee,
                assignee_groups=assignee_groups,
                on_locked="raise",
            )
        except kbd.BoardDispatchLockedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if getattr(args, "json", False):
        _print_json({
            **{k: getattr(res, k)
               for k in ("reclaimed", "crashed", "timed_out", "stale", "auto_blocked", "promoted")},
            "rate_limited": res.rate_limited,
            "output_limit": res.output_limit,
            "spawned": [
                {"task_id": tid, "assignee": who, "workspace": ws} for (tid, who, ws) in res.spawned
            ],
            "skipped_unassigned": res.skipped_unassigned,
            "skipped_nonspawnable": res.skipped_nonspawnable,
            "skipped_assignee_not_allowed": [
                {"task_id": tid, "assignee": who} for (tid, who) in res.skipped_assignee_not_allowed
            ],
            "skipped_per_profile_capped": [
                {"task_id": tid, "assignee": who, "current": current}
                for (tid, who, current) in res.skipped_per_profile_capped
            ],
            "capped_details": res.capped_details,
            "auto_assigned_default": res.auto_assigned_default,
        }, ascii=True)
        return 0
    print(f"Reclaimed:    {res.reclaimed}")
    for label, items in (
        ("Crashed:     ", res.crashed),
        ("Timed out:   ", res.timed_out),
        ("Stale:       ", res.stale),
        ("Auto-blocked:", res.auto_blocked),
    ):
        print(f"{label} {len(items)}")
        if items:
            print(f"  {', '.join(items)}")
    print(f"Promoted:     {res.promoted}")
    print(f"Spawned:      {len(res.spawned)}")
    tag = " (dry)" if args.dry_run else ""
    for tid, who, ws in res.spawned:
        print(f"  - {tid}  ->  {who}  @ {ws or '-'}{tag}")
    if res.auto_assigned_default:
        print(
            f"Auto-assigned to kanban.default_assignee={default_assignee!r}: "
            f"{', '.join(res.auto_assigned_default)}"
        )
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    for detail in res.capped_details:
        scope = detail["scope"]
        where = "per-profile cap" if scope == "assignee" else f"{scope} cap"
        print(
            f"Deferred ({detail['assignee']} at {where} {detail['cap']}, "
            f"{detail['running']} running): {detail['task_id']}"
        )
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — terminal lane, OK): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    for tid, who in res.skipped_assignee_not_allowed:
        print(f"Refused ({who} not in board allowed_assignees): {tid}")
    return 0


_DAEMON_DEPRECATED = (
    "hermes kanban daemon: DEPRECATED — the dispatcher now runs\ninside the gateway. To use "
    "kanban:\n\n    hermes gateway start       # starts the gateway + embedded dispatcher\n\nReady "
    "tasks will be picked up on the next dispatcher tick\n(default: every 60 seconds). Configure "
    "via config.yaml:\n\n    kanban:\n      dispatch_in_gateway: true      # default\n      "
    "dispatch_interval_seconds: 60\n      failure_limit: 2              # consecutive non-success "
    "attempts before auto-block\n\nRunning both the gateway AND this standalone daemon will\nrace "
    "for claims. If you truly need the old standalone\ndaemon (no gateway available), rerun with "
    "--force."
)


def stranded_warning(task_ids, *, now: int, state: dict, enabled: bool):
    """One WARN line naming ready cards whose assignee is not a Hermes profile, or ``None``.

    Upstream treats such an assignee as a control-plane lane pulled via ``claim_task`` and
    deliberately keeps the "stuck" warning quiet for it. An estate with no such lanes says
    so with ``kanban.require_known_assignee``; under that flag a ``skipped_nonspawnable``
    card is an error the operator must hear, not correctly-idle silence. Measured
    2026-09-23: 17 cards sat 21 days in ``ready`` beside a daemon that logged nothing.

    Rate-limited to once per 300s via ``state["last_stranded_warn_at"]``, like the stuck
    warning above it.
    """
    if not enabled or not task_ids:
        return None
    if now - int(state.get("last_stranded_warn_at", 0)) < 300:
        return None
    state["last_stranded_warn_at"] = now
    ids = ", ".join(task_ids)
    return (
        f"[{_fmt_ts(now)}] WARN {len(task_ids)} ready card(s) can never be dispatched: their "
        f"assignee is not a Hermes profile. {ids}. Reassign with `hermes kanban assign <id> "
        f"<profile>` (see `hermes kanban assignees`) or park them with `hermes kanban block`."
    )


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway. Kept so old
    scripts/systemd units get a clear migration message; ``--force`` (hidden
    from --help) keeps the standalone loop for hosts that truly cannot run the
    gateway. The default path exits 2 so nobody accidentally runs two
    dispatchers against the same kanban.db."""
    if not getattr(args, "force", False):
        return _err(_DAEMON_DEPRECATED, 2)

    # Init before printing "started" so the DB path is right and init errors
    # surface immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        try:
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force (interval={args.interval}s, "
        f"pid={os.getpid()}). Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry: warn when every tick finds ready work but spawns
    # nothing (broken profile, PATH drift, missing venv, credential loss) —
    # the per-task breaker auto-blocks quietly, so the operator needs a signal.
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    health_state = {"bad_ticks": 0, "last_warn_at": 0, "last_capped_by": {}, "last_stranded_warn_at": 0}
    strict_assignees = kb.require_known_assignee_enabled()

    def _ready_queue_nonempty() -> bool:
        """Is there a ready+assigned+unclaimed task the dispatcher would spawn for?
        Control-plane lanes pulled via ``claim_task`` are correctly idle, not stuck."""
        try:
            with kbc.connect_closing() as conn:
                return kbd.has_spawnable_ready(conn)
        except Exception:
            return False

    def _only_cap_deferred(res) -> bool:
        """Every spawnable ready task was deferred by a concurrency cap: the
        dispatcher is working and the queue is waiting for capacity, not stuck.
        Unassigned ready work is operator-actionable, so it still counts."""
        capped_ids = {tid for (tid, _who, _cur) in res.skipped_per_profile_capped}
        if not capped_ids or res.skipped_unassigned:
            return False
        try:
            with kbc.connect_closing() as conn:
                return not (kbd.spawnable_ready_ids(conn) - capped_ids)
        except Exception:
            return False

    def _on_tick(res):
        stranded = stranded_warning(res.skipped_nonspawnable, now=int(time.time()),
                                    state=health_state, enabled=strict_assignees)
        if stranded:
            print(stranded, file=sys.stderr, flush=True)
        ready_pending = bool(res.skipped_unassigned) or _ready_queue_nonempty()
        if ready_pending and not res.spawned and not _only_cap_deferred(res):
            health_state["bad_ticks"] += 1
        else:
            health_state["bad_ticks"] = 0
        # Warn once per HEALTH_WINDOW bad ticks, at most every 5 minutes.
        if health_state["bad_ticks"] >= HEALTH_WINDOW:
            now = int(time.time())
            if now - health_state["last_warn_at"] >= 300:
                print(
                    f"[{_fmt_ts(now)}] WARN dispatcher stuck: ready queue non-empty for "
                    f"{health_state['bad_ticks']} consecutive ticks but 0 workers spawned "
                    f"successfully. Check profile health (venv, PATH, credentials) and `hermes "
                    f"kanban list --status ready` / `hermes kanban list --status blocked` for "
                    f"recent spawn_failed tasks.",
                    file=sys.stderr, flush=True,
                )
                health_state["last_warn_at"] = now
        capped_by: dict = {}
        for _tid, who, _cur in res.skipped_per_profile_capped:
            capped_by[who] = capped_by.get(who, 0) + 1
        capped_changed = capped_by != health_state["last_capped_by"]
        health_state["last_capped_by"] = capped_by
        if not verbose:
            return
        did_work = (
            res.reclaimed or res.crashed or res.timed_out or res.promoted
            or res.spawned or res.auto_blocked or res.stale
        )
        # A changed cap picture prints once; an unchanged wait stays quiet.
        if did_work or (capped_by and capped_changed):
            capped_n = len(res.skipped_per_profile_capped)
            capped_txt = f"capped={capped_n}"
            if capped_n:
                capped_txt += " capped_by={" + ",".join(
                    f"{who}:{n}" for who, n in sorted(capped_by.items())
                ) + "}"
            print(
                f"[{_fmt_ts(int(time.time()))}] reclaimed={res.reclaimed} "
                f"crashed={len(res.crashed)} timed_out={len(res.timed_out)} stale={len(res.stale)} "
                f"promoted={res.promoted} spawned={len(res.spawned)} "
                f"auto_blocked={len(res.auto_blocked)} {capped_txt}",
                flush=True,
            )

    try:
        kbd.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            on_tick=_on_tick,
            max_in_progress_by_assignee=kbd.parse_assignee_caps(
                getattr(args, "max_per_assignee", None)
            ),
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()} if args.kinds else None
    print(f"Watching kanban events (initial board '{kb.get_current_board()}'). Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kbc.connect_closing() as conn:
        cursor = int(conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"])

    def tick():
        nonlocal cursor
        with kbc.connect_closing() as conn:
            rows = conn.execute(
                "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at,        t.assignee, "
                "t.tenant FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id WHERE e.id > ? "
                "ORDER BY e.id ASC LIMIT 200",
                (cursor,),
            ).fetchall()
        for r in rows:
            cursor = max(cursor, int(r["id"]))
            if (kinds and r["kind"] not in kinds) or (args.assignee and r["assignee"] != args.assignee) \
                    or (args.tenant and r["tenant"] != args.tenant):
                continue
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            pl = f" {payload}" if payload else ""
            print(
                f"[{_fmt_ts(r['created_at'])}] {r['task_id']:10s} "
                f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                flush=True,
            )

    return _poll_loop(args.interval, tick)


def _cmd_gc(args: argparse.Namespace) -> int:
    """Remove archived tasks' scratch workspaces, old events, and old worker logs."""
    import shutil
    scratch_root = kb.workspaces_root()
    removed_ws = 0
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, workspace_kind, workspace_path, branch_name FROM tasks "
            "WHERE status = 'archived'"
        ).fetchall()
    for row in rows:
        if row["workspace_kind"] == "worktree":
            # Backstop for worktrees that escaped the completion/archive hook.
            # Same safety predicate: only clean, fully-pushed worktrees go.
            wt_path = row["workspace_path"]
            if wt_path and Path(wt_path).is_dir():
                kbw._cleanup_worktree_workspace(row["id"], wt_path, row["branch_name"])
                if not Path(wt_path).is_dir():
                    removed_ws += 1
            continue
        if row["workspace_kind"] != "scratch":
            continue
        path = Path(row["workspace_path"] or (scratch_root / row["id"]))
        try:
            path = path.resolve()
        except OSError:
            continue
        try:
            path.relative_to(scratch_root.resolve())
        except ValueError:
            # Safety: never delete outside the scratch root.
            continue
        if path.exists() and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed_ws += 1

    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    with kbc.connect_closing() as conn:
        removed_events = kb.gc_events(conn, older_than_seconds=event_days * 24 * 3600)
    removed_logs = kb.gc_worker_logs(older_than_seconds=log_days * 24 * 3600)
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_logs} log file(s) removed")
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    """Integrity check + narrow index-REINDEX auto-repair. Dispatched BEFORE
    the auto ``kb.init_db()`` (init refuses corrupt DBs). Exit 0 = healthy /
    repaired / no DB file, 1 = still corrupt."""
    try:
        report = kbc.repair_db()
    except Exception as exc:  # locked/busy probe, unexpected I/O
        return _err(f"kanban repair: {exc}")

    if getattr(args, "json", False):
        _print_json({
            "status": report.status,
            "db_path": str(report.db_path),
            "messages": report.messages,
            "post_repair_messages": report.post_repair_messages,
            "backup_path": str(report.backup_path) if report.backup_path else None,
            "reindexed": report.reindexed,
        }, ascii=True)
        return 0 if report.status in {"ok", "repaired", "missing"} else 1

    if report.status == "missing":
        print(f"No kanban DB at {report.db_path} — nothing to repair.")
        return 0
    if report.status == "ok":
        print(f"{report.db_path}: integrity_check ok — no repair needed.")
        return 0
    if report.status == "repaired":
        print(f"{report.db_path}: repaired.")
        print(f"  reindexed: {', '.join(report.reindexed)}")
        if report.backup_path:
            print(f"  pre-repair backup: {report.backup_path}")
        print("  integrity_check now ok.")
        return 0
    # still corrupt
    def err(line: str) -> None:
        print(line, file=sys.stderr)

    err(f"{report.db_path}: CORRUPT.")
    for line in (report.messages or [])[:10]:
        err(f"  {line}")
    if report.reindexed:
        err(f"  REINDEX ({', '.join(report.reindexed)}) attempted but integrity_check is still failing:")
        for line in (report.post_repair_messages or [])[:10]:
            err(f"    {line}")
    else:
        err("  Not an index-only failure — automatic REINDEX repair does not apply (fail-closed).")
    if report.backup_path:
        err(f"  corrupt copy quarantined at: {report.backup_path}")
    err(
        "  Recover manually (copy kanban.db aside FIRST, then run "
        "`sqlite3 <copy> \".recover\"` into a fresh file — never against "
        "the live path, a WAL-reset-vulnerable sqlite3 CLI can corrupt it "
        "further) or move the file aside to start a new board."
    )
    return 1
