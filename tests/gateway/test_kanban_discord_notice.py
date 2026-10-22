"""Discord-specific Kanban notice format (anombyte93/hermes-agent#111).

Golden outputs per event kind for the Discord renderer. Notifier wiring and
the unchanged-output goldens for other platforms live in
test_kanban_notifier_discord_wiring.py.
"""

from gateway.kanban_notice_discord import (
    DISCORD_MESSAGE_LIMIT,
    clip_outcome,
    render_discord_kanban_notice,
)


def _render(kind, payload=None, **kw):
    args = dict(
        task_id="t_21c201fb",
        title="Verify: my-agents port of #332",
        assignee="claude",
        board="default",
        payload=payload or {},
        result=None,
    )
    args.update(kw)
    return render_discord_kanban_notice(kind, **args)


# ---------------------------------------------------------------- goldens


def test_completed_golden():
    assert _render("completed", {"summary": "Ported #332 onto capability-record.\nmore"}) == (
        "✅ **Verify: my-agents port of #332**\n"
        "Ported #332 onto capability-record.\n"
        "-# t_21c201fb · claude"
    )


def test_completed_falls_back_to_task_result():
    assert _render("completed", result="legacy result line\nignored") == (
        "✅ **Verify: my-agents port of #332**\n"
        "legacy result line\n"
        "-# t_21c201fb · claude"
    )


def test_completed_without_handoff_says_done():
    assert _render("completed") == (
        "✅ **Verify: my-agents port of #332**\n"
        "Done.\n"
        "-# t_21c201fb · claude"
    )


def test_blocked_golden():
    assert _render("blocked", {"reason": "needs a token"}) == (
        "🛑 **Verify: my-agents port of #332**\n"
        "Blocked: needs a token\n"
        "-# t_21c201fb · claude"
    )


def test_gave_up_golden():
    assert _render("gave_up", {"error": "spawn failed: ENOENT"}) == (
        "⚠️ **Verify: my-agents port of #332**\n"
        "Gave up after repeated spawn failures: spawn failed: ENOENT\n"
        "-# t_21c201fb · claude"
    )


def test_crashed_golden():
    assert _render("crashed") == (
        "⚠️ **Verify: my-agents port of #332**\n"
        "Worker crashed; the dispatcher will retry.\n"
        "-# t_21c201fb · claude"
    )


def test_timed_out_golden():
    assert _render("timed_out", {"limit_seconds": 7200}) == (
        "⚠️ **Verify: my-agents port of #332**\n"
        "Timed out after 7200s; will retry.\n"
        "-# t_21c201fb · claude"
    )


def test_status_running_is_started():
    assert _render("status", {"status": "running"}) == (
        "▶️ **Verify: my-agents port of #332**\n"
        "Started.\n"
        "-# t_21c201fb · claude"
    )


def test_status_other_golden():
    assert _render("status", {"status": "todo"}) == (
        "🔄 **Verify: my-agents port of #332**\n"
        "Moved to todo.\n"
        "-# t_21c201fb · claude"
    )


def test_review_requested_golden():
    assert _render("review_requested", {"summary": "Implemented X and verified."}) == (
        "👀 **Verify: my-agents port of #332**\n"
        "Ready for review: Implemented X and verified.\n"
        "-# t_21c201fb · claude"
    )


def test_changes_requested_golden():
    payload = {"reason": "Tests need updates", "reviewer": "rev", "implementer": "coder"}
    assert _render("changes_requested", payload) == (
        "🛑 **Verify: my-agents port of #332**\n"
        "Changes requested: Tests need updates\n"
        "-# t_21c201fb · reviewer rev → implementer coder"
    )


def test_block_loop_detected_golden():
    payload = {"reason": "same cause", "recurrences": 3}
    assert _render("block_loop_detected", payload) == (
        "🛑 **Verify: my-agents port of #332**\n"
        "Routed to triage, needs a human decision (blocked 3x for the same cause): same cause\n"
        "-# t_21c201fb · claude"
    )


def test_silent_kinds_render_nothing():
    assert _render("archived") is None
    assert _render("unblocked") is None


# ------------------------------------------------------------ behaviours


def test_board_shown_only_when_not_default():
    assert _render("crashed", board="ops").endswith("-# t_21c201fb · claude · board ops")
    assert "board" not in _render("crashed", board=None).splitlines()[-1]


def test_no_assignee_omits_it():
    assert _render("crashed", assignee=None).endswith("\n-# t_21c201fb")


def test_pr_url_is_masked_link():
    summary = "Opened https://github.com/anombyte93/moll-e/pull/283 with tests."
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == (
        "Opened [moll-e #283](https://github.com/anombyte93/moll-e/pull/283) with tests."
    )


def test_other_url_is_masked_not_raw():
    body = _render("completed", {"summary": "See https://example.com/a/b for logs."}).splitlines()[1]
    assert body == "See [example.com](https://example.com/a/b) for logs."


def test_mentions_are_neutralised():
    msg = _render(
        "completed",
        {"summary": "pinging @everyone and <@123> and <@&456> and @here"},
        title="hey @everyone",
        assignee="bob",
    )
    for raw in ("@everyone", "@here", "<@123>", "<@&456>"):
        assert raw not in msg
    assert "@\u200beveryone" in msg


def test_title_markdown_is_escaped():
    assert _render("crashed", title="a **b** _c_").splitlines()[0] == (
        "⚠️ **a \\*\\*b\\*\\* \\_c\\_**"
    )


def test_outcome_cut_at_sentence_boundary():
    first = "This is the first sentence of the handoff, and it is fairly long so it counts."
    text = first + " " + "Second sentence goes on and on " * 8
    out = clip_outcome(text, 180)
    assert out == first


def test_outcome_cut_at_clause_boundary():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda, " + "word " * 40
    out = clip_outcome(text, 180)
    assert out == "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda…"


def test_outcome_cut_at_word_boundary_never_mid_word():
    text = "word " * 60
    out = clip_outcome(text, 180)
    assert len(out) <= 180
    assert out.endswith("word…")


def test_outcome_short_text_untouched():
    assert clip_outcome("short.", 180) == "short."


def test_message_stays_under_discord_limit():
    many = " ".join(f"https://github.com/o/r/pull/{i}?{'x' * 300}" for i in range(40))
    msg = _render("completed", {"summary": many}, title="T" * 5000)
    assert len(msg) < DISCORD_MESSAGE_LIMIT
    assert "\n-# t_21c201fb" in msg


# ------------------------------------------- plain words (#112, card examples)


_STACKED_SUMMARY = (
    "Opened moll-e PR #305 (head 36778d0a4fbc9ae6ffc9ce53cf8440f7089eae26, "
    "stacked on #302 @ ca5c51bc, which is still open)."
)
_REVERIFY_SUMMARY = (
    "PASS on moll-e PR #304 re-verify at exact head "
    "c324d57056432526a380efb42eded5cb29bb1cf5. Merge of origin/main is clean."
)


def test_stacked_pr_summary_reads_in_plain_words():
    assert _render("completed", {"summary": _STACKED_SUMMARY}).splitlines()[1] == (
        "Opened moll-e PR #305."
    )


def test_reverify_summary_drops_exact_head_sha():
    assert _render("completed", {"summary": _REVERIFY_SUMMARY}).splitlines()[1] == (
        "PASS on moll-e PR #304 re-verify. Merge of origin/main is clean."
    )


def test_pr_reference_links_when_url_present():
    summary = _REVERIFY_SUMMARY.replace(
        "c324d57056432526a380efb42eded5cb29bb1cf5.",
        "c324d57056432526a380efb42eded5cb29bb1cf5 "
        "https://github.com/anombyte93/moll-e/pull/304.",
    )
    assert _render("completed", {"summary": summary}).splitlines()[1] == (
        "PASS on [moll-e #304](https://github.com/anombyte93/moll-e/pull/304) "
        "re-verify moll-e #304. Merge of origin/main is clean."
    )


def test_plain_words_outcome_keeps_no_machinery():
    for summary in (_STACKED_SUMMARY, _REVERIFY_SUMMARY):
        body = _render("completed", {"summary": summary}).splitlines()[1]
        assert "36778d0a" not in body
        assert "c324d570" not in body
        assert "ca5c51bc" not in body
        assert "@" not in body
        assert "()" not in body
        assert "exact head" not in body


def test_layout_lines_unchanged_by_plain_words():
    msg = _render("completed", {"summary": _STACKED_SUMMARY}).splitlines()
    assert msg[0] == "✅ **Verify: my-agents port of #332**"
    assert msg[2] == "-# t_21c201fb · claude"


def test_outcome_keeps_at_most_two_sentences():
    summary = (
        "First sentence lands the outcome. "
        "Second sentence adds the check result and passes the gate. "
        "Third sentence drones on about merge trees and should never appear."
    )
    assert clip_outcome(summary) == (
        "First sentence lands the outcome. "
        "Second sentence adds the check result and passes the gate."
    )


def test_paths_are_dropped_from_outcome():
    summary = (
        "Fixed the renderer in gateway/kanban_notice_discord.py and updated "
        "~/atlas/worktrees/moll-e/docs; /tmp/build.log stays behind."
    )
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == "Fixed the renderer in and updated; stays behind."


def test_branch_refs_survive_the_path_filter():
    summary = "Merge of origin/main into feat/110-112-on-deployed is clean."
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == "Merge of origin/main into feat/110-112-on-deployed is clean."

