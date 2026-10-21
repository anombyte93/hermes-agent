"""Discord-friendly rendering for Kanban notifier pings (anombyte93/hermes-agent#111).

The gateway Kanban notifier (``gateway/kanban_watchers.py``) builds one
plain-text line per terminal event. That legacy line leads with machinery
(``[board] @assignee Kanban t_xxx done —``) and truncates the title, which
reads badly on a phone. For ``platform == "discord"`` only, the notifier
swaps the legacy text for the layout rendered here::

    ✅ **Card title**
    The outcome in plain words, cut at a sentence or clause boundary.
    -# t_21c201fb · claude · board ops

Rules:

* line 1: one state marker, then the title in bold (Markdown-escaped);
* line 2: the outcome, at most ~180 visible characters, cut at a sentence
  or clause boundary and never mid-word;
* line 3: Discord subtext (``-# ``) with the card id, the assignee and the
  board only when it is not ``default``;
* URLs never appear raw: a GitHub PR URL becomes ``[PR #N](url)`` and any
  other URL ``[host](url)``;
* ``@everyone``/``@here`` and ``<@id>``/``<@&id>``/``<#id>`` mention syntax
  is neutralised with a zero-width space, so nothing in the text can ping;
* the whole message stays under Discord's 2000-character limit.

Every other platform keeps the legacy text byte-for-byte; this module is
pure (no I/O) and is not involved in delivery, dedupe or wake.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

DISCORD_MESSAGE_LIMIT = 2000
OUTCOME_LIMIT = 180
TITLE_LIMIT = 200
_SAFE_LIMIT = DISCORD_MESSAGE_LIMIT - 50

_ZWSP = "\u200b"

MARKERS = {
    "completed": "✅",
    "blocked": "🛑",
    "changes_requested": "🛑",
    "block_loop_detected": "🛑",
    "gave_up": "⚠️",
    "crashed": "⚠️",
    "timed_out": "⚠️",
    "review_requested": "👀",
    "started": "▶️",
    "status": "🔄",
}

_URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")
_URL_TRAILING = ".,;:!?'\""
_GITHUB_PR_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/[^/\s]+/[^/\s]+/pull/(\d+)(?:[/?#].*)?$"
)
_MASKED_LINK_RE = re.compile(r"\[[^\]\n]*\]\((https?://[^)\s]+)\)")
_MENTION_RE = re.compile(r"<(@[!&]?|#)(\d+)>")
_EVERYONE_RE = re.compile(r"@(everyone|here)\b")
_MD_SPECIAL_RE = re.compile(r"([\\*_~`|])")

_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
_CLAUSE_RE = re.compile(r"\s*(?:[,;:]|\s[—–-])(?=\s)")


def _neutralise(text: str) -> str:
    """Break every Discord mention form so it renders but cannot ping."""
    text = _EVERYONE_RE.sub(lambda m: "@" + _ZWSP + m.group(1), text)
    return _MENTION_RE.sub(lambda m: "<" + m.group(1) + _ZWSP + m.group(2) + ">", text)


def _one_line(text: Any) -> str:
    """First non-empty line of ``text`` with whitespace collapsed."""
    if text is None:
        return ""
    for line in str(text).strip().splitlines():
        line = " ".join(line.split())
        if line:
            return line
    return ""


def _link_label(url: str) -> str:
    m = _GITHUB_PR_RE.match(url)
    if m:
        return f"PR #{m.group(1)}"
    host = urlparse(url).netloc or "link"
    return host[4:] if host.startswith("www.") else host


def _segments(text: str) -> list[tuple[str, Optional[str]]]:
    """Split ``text`` into (visible_text, url) runs; url is None for prose.

    Existing masked links ``[label](url)`` and bare URLs both become link
    segments, so a link is always one atomic unit for clipping.
    """
    segs: list[tuple[str, Optional[str]]] = []
    pos = 0
    matches = []
    for m in _MASKED_LINK_RE.finditer(text):
        label = m.group(0)[1 : m.group(0).index("](")]
        matches.append((m.start(), m.end(), label, m.group(1)))
    taken = [(s, e) for s, e, _, _ in matches]
    for m in _URL_RE.finditer(text):
        if any(s <= m.start() < e for s, e in taken):
            continue
        url = m.group(0).rstrip(_URL_TRAILING)
        matches.append((m.start(), m.start() + len(url), _link_label(url), url))
    matches.sort()
    for start, end, label, url in matches:
        if start > pos:
            segs.append((text[pos:start], None))
        segs.append((label, url))
        pos = end
    if pos < len(text):
        segs.append((text[pos:], None))
    return segs


def _cut_index(visible: str, limit: int, forbidden: set[int]) -> tuple[int, bool]:
    """Return (cut, add_ellipsis) for clipping ``visible`` to ``limit``.

    Prefers a sentence end, then a clause boundary, then a word boundary;
    a cut never lands inside a link label (``forbidden`` positions).
    """
    floor = int(limit * 0.3)
    window = visible[:limit]
    best = -1
    for m in _SENTENCE_END_RE.finditer(window):
        if m.end() >= floor and m.end() not in forbidden:
            best = m.end()
    if best > 0:
        return best, False
    window = visible[: limit - 1]
    for m in _CLAUSE_RE.finditer(window):
        if m.start() >= floor and m.start() not in forbidden:
            best = m.start()
    if best > 0:
        return best, True
    for i in range(len(window), 0, -1):
        if i < len(visible) and visible[i].isspace() and i not in forbidden:
            return len(visible[:i].rstrip()), True
    for i in range(limit - 1, 0, -1):
        if i not in forbidden:
            return i, True
    return 0, True


def clip_outcome(text: str, limit: int = OUTCOME_LIMIT, budget: int = _SAFE_LIMIT) -> str:
    """Render ``text`` as one Discord line: masked links, clipped, no pings.

    ``limit`` bounds the *visible* length (link labels count, URLs do not);
    ``budget`` bounds the rendered length — links that would overflow it are
    emitted as their bare label.
    """
    segs = _segments(_one_line(text))
    visible = "".join(label for label, _ in segs)
    keep = len(visible)
    ellipsis = False
    if len(visible) > limit:
        forbidden: set[int] = set()
        pos = 0
        for label, url in segs:
            if url is not None:
                forbidden.update(range(pos + 1, pos + len(label)))
            pos += len(label)
        keep, ellipsis = _cut_index(visible, limit, forbidden)

    out: list[str] = []
    pos = 0
    rendered = 0
    for label, url in segs:
        if pos >= keep:
            break
        piece = label[: keep - pos]
        pos += len(label)
        if url is None:
            chunk = _neutralise(piece)
        else:
            label_md = _neutralise(piece.replace("]", ")").replace("[", "("))
            chunk = f"[{label_md}]({url})"
            if rendered + len(chunk) > budget:
                chunk = label_md
        out.append(chunk)
        rendered += len(chunk)
    result = "".join(out).rstrip()
    if ellipsis:
        result = result.rstrip(" ,;:—–-") + "…"
    return result


def _title_line(marker: str, title: str) -> str:
    title = " ".join(str(title or "").split())
    if len(title) > TITLE_LIMIT:
        cut = title.rfind(" ", 0, TITLE_LIMIT - 1)
        title = title[: cut if cut > TITLE_LIMIT // 2 else TITLE_LIMIT - 1].rstrip() + "…"
    title = _MD_SPECIAL_RE.sub(r"\\\1", title)
    return f"{marker} **{_neutralise(title)}**"


def _subtext(parts: list[str]) -> str:
    clean = [_neutralise(" ".join(p.split())) for p in parts if p and p.strip()]
    line = " · ".join(clean)
    if len(line) > 200:
        line = line[:199] + "…"
    return f"-# {line}"


def render_discord_kanban_notice(
    kind: str,
    *,
    task_id: str,
    title: Optional[str],
    assignee: Optional[str],
    board: Optional[str],
    payload: Optional[Mapping[str, Any]],
    result: Optional[str] = None,
) -> Optional[str]:
    """Return the Discord text for one Kanban event, or None for silent kinds."""
    payload = payload or {}
    who = assignee or ""
    marker_kind = kind

    if kind == "completed":
        outcome = _one_line(payload.get("summary")) or _one_line(result) or "Done."
    elif kind == "blocked":
        reason = _one_line(payload.get("reason"))
        outcome = f"Blocked: {reason}" if reason else "Blocked."
    elif kind == "gave_up":
        err = _one_line(payload.get("error"))
        outcome = "Gave up after repeated spawn failures" + (f": {err}" if err else ".")
    elif kind == "crashed":
        outcome = "Worker crashed; the dispatcher will retry."
    elif kind == "timed_out":
        try:
            limit = int(payload.get("limit_seconds") or 0)
        except (TypeError, ValueError):
            limit = 0
        outcome = f"Timed out after {limit}s; will retry."
    elif kind == "status":
        new_status = _one_line(payload.get("status"))
        if new_status == "running":
            marker_kind = "started"
            outcome = "Started."
        else:
            outcome = f"Moved to {new_status}." if new_status else "Status changed."
    elif kind == "review_requested":
        summary = _one_line(payload.get("summary"))
        outcome = f"Ready for review: {summary}" if summary else "Ready for review."
    elif kind == "changes_requested":
        # ``_safe_review_reason`` moved to ``kanban_watchers_notifier`` in the
        # deployed split; the fallback keeps the module importable in both layouts.
        try:
            from gateway.kanban_watchers_notifier import _safe_review_reason
        except ImportError:
            from gateway.kanban_watchers import _safe_review_reason

        reason = _safe_review_reason(payload.get("reason"), 400)
        outcome = f"Changes requested: {reason or 'reviewer feedback requires changes'}"
        reviewer = _safe_review_reason(payload.get("reviewer"), 48)
        implementer = _safe_review_reason(payload.get("implementer"), 48)
        roles = []
        if reviewer:
            roles.append(f"reviewer {reviewer}")
        if implementer:
            roles.append(f"implementer {implementer}")
        who = " → ".join(roles)
    elif kind == "block_loop_detected":
        rec = payload.get("recurrences")
        rc = f" (blocked {rec}x for the same cause)" if rec else ""
        reason = _one_line(payload.get("reason"))
        outcome = f"Routed to triage, needs a human decision{rc}" + (
            f": {reason}" if reason else "."
        )
    else:
        return None

    head = _title_line(MARKERS[marker_kind], title or task_id)
    board_part = f"board {board}" if board and board != "default" else ""
    sub = _subtext([task_id, who, board_part])
    budget = DISCORD_MESSAGE_LIMIT - len(head) - len(sub) - 10
    body = clip_outcome(outcome, OUTCOME_LIMIT, budget)
    msg = f"{head}\n{body}\n{sub}" if body else f"{head}\n{sub}"
    if len(msg) >= DISCORD_MESSAGE_LIMIT:  # defensive; budgets above prevent it
        msg = f"{head}\n{sub}"[: DISCORD_MESSAGE_LIMIT - 1]
    return msg
