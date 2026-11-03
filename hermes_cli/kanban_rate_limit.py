"""Provider-limit log evidence and reset-aware Kanban retry deadlines.

This is a dispatcher backstop, not an API error classifier. Live turns use
agent.error_classifier; old workers can only leave their terminal log behind.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from agent.retry_utils import reset_delay_from_message

_LOG_TAIL_BYTES = 64 * 1024
_RUN_MARKER = re.compile(rb"\n\[kanban run (\d+)\]\r?\n")
_HTTP_429 = re.compile(r"\bHTTP\s+429\b", re.I)
_LIMIT_MARKER = re.compile(r"usage\s+limit|rate\s+limit|quota|rate_limit_error", re.I)
_ROUTE = re.compile(r"Provider:\s*(\S+)\s+Model:\s*(\S+)")
_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I,
)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_RESET_PREFIX = re.compile(r"\b(?:resets?\s+(?:at|in|after)|try\s+again\s+in|retry[- ]after)\s*[:=]?\s*", re.I)
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|\s*UTC|[+-]\d{2}:?\d{2})?", re.I)


def worker_log_tail(task_id: str, *, run_id: Optional[int], board: Optional[str] = None) -> str:
    """Bound I/O and discard earlier attempts, even in a short append-mode log.

    Pre-marker workers retain the bounded-tail fallback. Missing or unreadable
    evidence never upgrades a crash to a failure-free provider-limit retry.
    """
    from hermes_cli import kanban_db as kb

    try:
        path = kb.worker_logs_dir(board=board) / f"{task_id}.log"
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - _LOG_TAIL_BYTES))
            data = stream.read(_LOG_TAIL_BYTES)
    except (OSError, ValueError):
        return ""
    markers = list(_RUN_MARKER.finditer(data))
    if markers:
        last = markers[-1]
        if run_id is not None and int(last[1]) != run_id:
            return ""
        data = data[last.end():]
    return data.decode("utf-8", errors="replace")


def contains_provider_limit(text: str) -> bool:
    return bool(_HTTP_429.search(text) and _LIMIT_MARKER.search(text))


def _absolute_reset(value: str) -> Optional[int]:
    match = _ISO_TIME.match(value)
    try:
        if match:
            stamp = match[0].upper().replace(" UTC", "+00:00").replace("Z", "+00:00")
            parsed = datetime.fromisoformat(stamp)
        else:
            # Retry-After's HTTP-date form. Never parse the process locale's time.
            parsed = parsedate_to_datetime(value.split("\n", 1)[0].strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _deadline_after(seconds: float, now: int) -> Optional[int]:
    if not math.isfinite(seconds) or seconds < 0:
        return None
    until = now + math.ceil(seconds)
    try:
        datetime.fromtimestamp(until, timezone.utc)
    except (ValueError, OverflowError, OSError):
        # Malformed provider text must not abort the whole reclaim transaction.
        return None
    return until


def reset_epoch(text: str, now: int) -> Optional[int]:
    """Read explicit reset/Retry-After wording before considering quota windows."""
    prefixes = list(_RESET_PREFIX.finditer(text))
    for prefix in prefixes:
        absolute = _absolute_reset(text[prefix.end():].strip())
        if absolute is not None:
            return max(now, absolute)
    # Reuse the agent/pool's reset grammar and precedence, including
    # quotaResetDelay and fractional seconds. Only the extra wordings below
    # (try again in, hyphenated Retry-After, day durations) need supplementation.
    # The shared grammar accepts contiguous units, not the prose conjunction.
    delay_text = re.sub(r"\band\s+(?=\d)", "", text, flags=re.I)
    seconds = reset_delay_from_message(delay_text)
    if seconds is not None and (until := _deadline_after(seconds, now)) is not None:
        return until
    for prefix in prefixes:
        value = text[prefix.end():].strip()
        # Consume only contiguous duration components, not unrelated numbers later
        # in the error (a plan URL or a retry counter, for example).
        seconds = 0.0
        matched = False
        rest = value
        while match := _DURATION_PART.match(rest):
            matched = True
            seconds += float(match[1]) * _UNIT_SECONDS[match[2][0].lower()]
            rest = rest[match.end():].lstrip(" ,")
            if rest.lower().startswith("and "):
                rest = rest[4:]
        if matched and (until := _deadline_after(seconds, now)) is not None:
            return until
        if re.fullmatch(r"retry[- ]after\s*[:=]?\s*", prefix[0], re.I):
            match = re.match(r"(\d+(?:\.\d+)?)(?=\s|$|[.;,])", value)
            if match and (until := _deadline_after(float(match[1]), now)) is not None:
                return until
    return None


def _streak(conn, task_id: str, now: int) -> tuple[int, int]:
    count, first = 1, now
    for row in conn.execute(
        "SELECT outcome, ended_at FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL ORDER BY id DESC",
        (task_id,),
    ):
        if row["outcome"] != "rate_limited":
            break
        count += 1
        first = int(row["ended_at"])
    return count, first


def retry_deadline(conn, task_id: str, text: str, now: int, cooldown: int) -> int:
    """Explicit reset > named quota window > consecutive-run backoff ladder."""
    if cooldown <= 0:
        return now
    explicit = reset_epoch(text, now)
    if explicit is not None:
        return explicit
    count, first = _streak(conn, task_id, now)
    backoff = (cooldown, 3 * cooldown, 12 * cooldown)[min(count - 1, 2)]
    if re.search(r"\b5[-\s]hour\b", text, re.I):
        # The first deadline must not slide on early/manual retries. Once it has
        # elapsed, a still-capped probe uses backoff rather than a next-tick loop.
        deadline = first + 5 * 3600
        return deadline if now < deadline else now + backoff
    if re.search(r"\bweekly\b", text, re.I):
        return now + 24 * 3600
    return now + backoff


def provider_limit_details(conn, task_id: str, text: str, now: int, cooldown: int) -> tuple[str, dict]:
    """Safe board copy plus durable deadline and route facts (no raw provider body)."""
    row = conn.execute(
        "SELECT t.provider_override, t.model_override, r.provider_used, r.model_used "
        "FROM tasks t LEFT JOIN task_runs r ON r.id=t.current_run_id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    routes = _ROUTE.findall(text)
    provider, model = routes[-1] if routes else (
        row["provider_used"] or row["provider_override"] or "unknown",
        row["model_used"] or row["model_override"] or "unknown",
    )
    until = retry_deadline(conn, task_id, text, now, cooldown)
    stamp = datetime.fromtimestamp(until, timezone.utc).isoformat().replace("+00:00", "Z")
    error = f"provider usage limit (HTTP 429) on {provider}/{model}; next try after {stamp}"
    return error, {"provider": provider, "model": model, "until": until, "rate_limit_until": until}
