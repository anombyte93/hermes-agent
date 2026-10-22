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
* line 2: the outcome in plain words — git SHAs (with any ``at`` /
  ``exact`` / ``head`` / ``@`` scaffolding), absolute and ``~/`` paths,
  the connectors those drops leave dangling, parentheses emptied by
  them and sentences reduced to the removed machinery are removed;
  a bare hex token counts as a SHA only when it mixes letters and
  digits (counts like ``10000000`` and words like ``deadbeef`` stay),
  ``PR #N`` references render as ``repo #N`` once (a masked link when
  the summary carries the repo's GitHub pull URL; a URL that only
  restates an already-linked reference is dropped), and at most the
  first two sentences are kept, cut at a sentence or clause boundary
  never mid-word;
* line 3: Discord subtext (``-# ``) with the card id, the assignee and the
  board only when it is not ``default``;
* URLs never appear raw: a GitHub PR URL becomes ``[repo #N](url)`` and any
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

# --- plain-words pass (#112): machinery a phone reader does not want -------

# Removed machinery is marked with a hole sentinel so later passes can see
# what went missing and clean up the words it left dangling.
_HOLE = "\x00"

# A git SHA candidate: 7 to 40 hex chars standing alone (never a fraction).
# Only a mixed token (a letter AND a digit) is taken for a SHA on its own;
# pure numbers ("10000000") and pure words ("deadbeef") stay.
_SHA_RE = re.compile(r"(?<![\d.])\b[0-9a-f]{7,40}\b")
# The same SHA together with the words that only ever introduce one.
_SHA_INTRO_RE = re.compile(
    r"\b(?:at|exact|head|commit|sha|treeish)\s+(?:at\s+|head\s+|exact\s+)*"
    r"(?:0x\s*)?[0-9a-f]{7,40}\b",
    re.IGNORECASE,
)
# Trailing "at" / "head" / "exact head" left dangling beside a hole.
_AT_HEAD_RE = re.compile(r"\b(?:exact\s+head|head|at)\s*" + _HOLE, re.IGNORECASE)
# A dangling "@" (as in "stacked on #302 @ <sha>") immediately before a hole.
_ORPHAN_AT_RE = re.compile(r"\s*@\s*" + _HOLE)
# Absolute (starting "~" or "/") or multi-segment file paths. Single-slash
# candidates without a file extension (origin/main, feat/foo) look like branch
# refs and are kept; the path filter below decides.
_PATH_RE = re.compile(r"(?<![\w~])(?:~/?|/?)[\w.-]+(?:/[\w.-]+)+")
_EXT_RE = re.compile(r"\.\w{1,4}\Z")
# A connector or comma run sitting between two holes went with them.
_BETWEEN_HOLES_RE = re.compile(_HOLE + r"[\s,;]*(?:and|or|nor|but)?[\s,;]*(?=" + _HOLE + r")")
# A preposition whose object was a hole ("renderer in <path>") goes too.
_DANGLING_PREP_RE = re.compile(
    r"\s+(?:in|on|at|to|into|from|with|by|of|for|via|using)\s+(?=" + _HOLE + r")"
)
# "PR #N", "moll-e PR #N", "pull request #N" — an optional repo qualifier.
_PR_REF_RE = re.compile(r"\b(?:(\w[\w.-]*)\s+)?(?:PR|pr|pull request|pull)\s*#?(\d+)\b")
_PR_LABEL_RE = re.compile(r"^PR #(\d+)$")
_HOLLOW_PAREN_RE = re.compile(r"\(([^()]*)\)")
_PAREN_LEAD_WORDS = frozenset({"which", "who", "and", "but", "so", "stacked"})
# Words that only ever introduce dropped machinery inside a parenthetical.
_PAREN_SEE_WORDS = frozenset({"see", "including", "via", "using", "at", "in", "on"})
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TYPO_FIX_RE = re.compile(r"\b(\w+) (?:at|@) \1\b")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?)])")
# A colon/comma left holding hands with the sentence's full stop.
_ORPHAN_PUNCT_RE = re.compile(r"[\s,:;]+\s*(?=\.)")
_GITHUB_PULL_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)(?:[/?#][^\s]*)?"
)


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


def _plain_link_label(label: str, url: Optional[str], repos: Mapping[str, Mapping[str, str]]) -> tuple[str, Optional[str]]:
    """Relabel a GitHub PR link as ``repo #N`` for the plain-words pass."""
    m = _PR_LABEL_RE.match(label)
    if m is None or url is None:
        return label, url
    hit = _link_target(url)
    if hit is None:
        return label, url
    canonical, owner_repo, _ = hit
    number = m.group(1)
    if number in (repos.get(owner_repo) or {}):
        name = owner_repo.split("/", 1)[1]
        return f"{name} #{number}", canonical
    return label, url


def _link_target(url: str) -> Optional[tuple[str, str, str]]:
    """(canonical_url, "owner/repo", number) for a GitHub pull URL, else None."""
    m = _GITHUB_PULL_URL_RE.fullmatch(url)
    if m:
        canonical = f"https://github.com/{m.group(1)}/{m.group(2)}/pull/{m.group(3)}"
        return canonical, f"{m.group(1).lower()}/{m.group(2).lower()}", m.group(3)
    return None


def _pr_repos(segs: list[tuple[str, Optional[str]]]) -> dict[str, dict[str, str]]:
    """``owner/repo`` -> {pr number: canonical pull URL} for links in the text."""
    repos: dict[str, dict[str, str]] = {}
    for _label, url in segs:
        if url is None:
            continue
        hit = _link_target(url)
        if hit:
            canonical, owner_repo, number = hit
            repos.setdefault(owner_repo, {})[number] = canonical
    return repos


def _is_sha(token: str) -> bool:
    """A SHA carries both hex letters and digits; "10000000" and "deadbeef"
    are a count and a word, not commits, and stay."""
    return any(c in "abcdef" for c in token.lower()) and any(c.isdigit() for c in token)


def _remove_with_scaffold(
    rx: re.Pattern, text: str, keep=None
) -> str:
    """Remove every ``rx`` match, leaving a ``_HOLE`` sentinel that a later
    pass trades for the dangling ``at``/``head``/``@`` scaffolding beside it
    (``re-verify at <sha>``, ``#302 @ <sha>``) and the connectors between
    holes.
    """
    out: list[str] = []
    pos = 0
    for m in rx.finditer(text):
        if keep is not None and keep(m.group(0)):
            continue
        out.append(text[pos : m.start()])
        out.append(_HOLE)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _is_file_path(token: str) -> bool:
    """Paths start absolute or carry an extension; branch refs stay."""
    return (
        token.startswith(("~", "/"))
        or bool(_EXT_RE.search(token))
        or token.count("/") >= 2
    )


def _paren_is_machinery(inner: str) -> bool:
    """A parenthetical that only scaffolded dropped machinery goes too."""
    s = inner.strip()
    if not s or _HOLE in s:
        return True
    words = s.split()
    if words[0].lower() in _PAREN_LEAD_WORDS or words[0].lower() in _PAREN_SEE_WORDS:
        return True
    return all(w.lower() in _PAREN_LEAD_WORDS | _PAREN_SEE_WORDS for w in words)


def _drop_hollow_parens(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _HOLLOW_PAREN_RE.sub(
            lambda m: "" if _paren_is_machinery(m.group(1)) else m.group(0), text
        )
    return text


def _clean_hole_words(text: str) -> str:
    """Remove the words that only held removed machinery together: connectors
    and commas between holes, and prepositions whose object was a hole."""
    prev = None
    while prev != text:
        prev = text
        text = _BETWEEN_HOLES_RE.sub(_HOLE, text)
        text = _DANGLING_PREP_RE.sub("", text)
        text = _AT_HEAD_RE.sub(_HOLE, text)
        text = _ORPHAN_AT_RE.sub(_HOLE, text)
    return text


def _collapse_sentence(text: str) -> str:
    """Drop a sentence the scrub emptied of content: when >=2 holes gutted it
    down to a couple of filler words, what was removed WAS the sentence."""
    kept = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        holes = sentence.count(_HOLE)
        body = sentence.replace(_HOLE, " ")
        words = [w for w in body.split() if any(ch.isalnum() for ch in w)]
        if holes >= 2 and len(words) <= 2:
            continue
        kept.append(sentence)
    return " ".join(kept)


def _scrub(text: str) -> str:
    """Drop SHAs (with their introductory words) and file paths, then the
    words left dangling by those drops."""
    text = _remove_with_scaffold(_SHA_INTRO_RE, text)
    text = _remove_with_scaffold(_SHA_RE, text, keep=lambda t: not _is_sha(t))
    text = _remove_with_scaffold(_PATH_RE, text, keep=lambda t: not _is_file_path(t))
    text = _clean_hole_words(text)
    text = _drop_hollow_parens(text)
    text = _collapse_sentence(text)
    text = text.replace(_HOLE, " ")
    text = _TYPO_FIX_RE.sub(r"\1", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _ORPHAN_PUNCT_RE.sub("", text)
    return " ".join(text.split()).strip(" ,;:—–-&@")


def _pr_ref_link(
    qual: Optional[str], number: str, repos: Mapping[str, Mapping[str, str]]
) -> Optional[tuple[str, str]]:
    """(label, url) for a PR reference, or None to leave it as written."""
    if qual:
        for owner_repo, numbers in repos.items():
            if owner_repo.split("/", 1)[1] == qual.lower() and number in numbers:
                return f"{qual} #{number}", numbers[number]
        return None
    if len(repos) == 1:
        owner_repo, numbers = next(iter(repos.items()))
        if number in numbers:
            name = owner_repo.split("/", 1)[1]
            return f"{name} #{number}", numbers[number]
    return None


def _rewrite_pr_refs(
    text: str,
    repos: Mapping[str, Mapping[str, str]],
    seen: set[str],
) -> list[tuple[str, Optional[str]]]:
    """Turn prose ``PR #N`` references into ``repo #N`` masked-link segments."""
    if not text or not repos:
        return [(text, None)] if text else []
    out: list[tuple[str, Optional[str]]] = []
    pos = 0
    for m in _PR_REF_RE.finditer(text):
        hit = _pr_ref_link(m.group(1), m.group(2), repos)
        if hit is None or hit[1] in seen:
            continue
        label, url = hit
        if m.start() > pos:
            out.append((text[pos : m.start()], None))
        out.append((label, url))
        seen.add(url)
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], None))
    return out or [(text, None)]


def plainwords_outcome(
    segs: list[tuple[str, Optional[str]]]
) -> list[tuple[str, Optional[str]]]:
    """Plain-words pass (#112): no SHAs, no paths, ``PR #N`` as ``repo #N``.

    A GitHub pull URL present anywhere in the outcome supplies the repo name,
    so ``PR #305`` plus the URL becomes the masked link ``moll-e #305``;
    without a URL the reference keeps the worker's own words. The same PR
    is linked once — a duplicate link that only repeats an already-linked
    reference (``PR #121: <url>``) is dropped with its introducing ``:``
    rather than rendered as a second label.
    """
    repos = _pr_repos(segs)
    seen: set[str] = set()
    out: list[tuple[str, Optional[str]]] = []
    for label, url in segs:
        if url is None:
            pieces = _rewrite_pr_refs(_scrub(label), repos, seen)
        else:
            new_label, new_url = _plain_link_label(label, url, repos)
            if new_url is not None:
                if new_url in seen:
                    # Duplicate of an already-linked reference: when the
                    # preceding prose ends with ':' or ',', the link was
                    # only the reference restated, and both go.
                    new_label, new_url = "", None
                    if out and out[-1][1] is None and out[-1][0].rstrip().endswith((":", ",")):
                        out[-1] = (out[-1][0].rstrip()[:-1].rstrip(), None)
                else:
                    seen.add(new_url)
            pieces = [(new_label, new_url)] if new_label else []
        for piece in pieces:
            if out and piece[0] and out[-1][0]:
                glue = out[-1][0][-1]
                if not glue.isspace() and piece[0][0].isalnum():
                    out.append((" ", None))
            out.append(piece)
    # Drop segments emptied by the duplicate fix, and a now-dangling colon
    # at the end of the kept text.
    out = [(label, url) for label, url in out if label]
    while out and out[-1][1] is None and out[-1][0].rstrip().endswith((":", ",", " ")):
        stripped = out[-1][0].rstrip().rstrip(":,").rstrip()
        if not stripped:
            out.pop()
        else:
            out[-1] = (stripped, None)
            break
    return out


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


def _first_sentences(visible: str, count: int = 2) -> tuple[int, bool]:
    """Cut ``visible`` to its first ``count`` sentence ends (if any).

    Returns (keep_index, ellipsis). When the text holds fewer than ``count``
    sentence ends, the whole text is kept (keep == len(visible), ellipsis
    False) — length clipping still applies afterwards.
    """
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(visible)]
    if len(ends) < count:
        return len(visible), False
    return ends[count - 1], True


def clip_outcome(text: str, limit: int = OUTCOME_LIMIT, budget: int = _SAFE_LIMIT) -> str:
    """Render ``text`` as one Discord line: masked links, clipped, no pings.

    The plain-words pass (#112) runs first: git SHAs and file paths are
    dropped with their scaffolding, and ``PR #N`` becomes ``repo #N`` when
    the outcome carries the repo's GitHub pull URL. ``limit`` bounds the
    *visible* length (link labels count, URLs do not); ``budget`` bounds
    the rendered length — links that would overflow it are emitted as
    their bare label.
    """
    segs = plainwords_outcome(_segments(_one_line(text)))
    visible = "".join(label for label, _ in segs)
    keep, _ = _first_sentences(visible, 2)
    if keep < len(visible) or len(visible) > limit:
        forbidden: set[int] = set()
        pos = 0
        for label, url in segs:
            if url is not None:
                forbidden.update(range(pos + 1, pos + len(label)))
            pos += len(label)
        keep = min(keep, len(visible))
        if len(visible) > limit:
            cut, _ = _cut_index(visible, limit, forbidden)
            keep = min(keep, cut)
    ellipsis = keep < len(visible) and visible[keep - 1] not in ".!?"

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
