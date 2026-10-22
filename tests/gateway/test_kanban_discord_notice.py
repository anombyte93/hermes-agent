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
    # The prose reference becomes the masked link; the URL segment that
    # trailed it was the same PR restated, so it is dropped (F1), not
    # rendered again as a plain label.
    assert _render("completed", {"summary": summary}).splitlines()[1] == (
        "PASS on [moll-e #304](https://github.com/anombyte93/moll-e/pull/304) "
        "re-verify. Merge of origin/main is clean."
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


def test_paths_render_as_file_names_not_deletions():
    summary = (
        "Fixed the renderer in gateway/kanban_notice_discord.py and updated "
        "~/atlas/worktrees/moll-e/docs; /tmp/build.log stays behind."
    )
    # A path is never deleted outright — that leaves dangling grammar
    # (#121 live check). A file path becomes its file name in backticks,
    # a directory path its last segment plus "/".
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == (
        "Fixed the renderer in `kanban_notice_discord.py` and updated "
        "`docs/`; `build.log` stays behind."
    )


def test_proof_example_keeps_its_grammar():
    # The exact live-check example (#121 proof msg 1562840413453164556):
    # the path must not be deleted, and the sentence's trailing period
    # must survive outside the backticks (it once made the file name
    # look like a directory: `proof.py./`).
    summary = (
        "Proof: opened moll-e PR #305 "
        "(head 36778d0a4fbc9ae6ffc9ce53cf8440f7089eae26) and wrote /home/x/proof.py."
    )
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == "Proof: opened moll-e PR #305 and wrote `proof.py`."


def test_branch_refs_survive_the_path_filter():
    summary = "Merge of origin/main into feat/110-112-on-deployed is clean."
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == "Merge of origin/main into feat/110-112-on-deployed is clean."


# ------------------------------- edge cases (review findings on #121, F1-F3)


_DOUBLED_LABEL_SUMMARY = (
    "Opened hermes-agent PR #121: "
    "https://github.com/anombyte93/hermes-agent/pull/121 (head 30e81b5c...)."
)


def test_f1_pr_label_renders_once_not_twice():
    # The prose reference becomes the masked link; the link segment that
    # followed "PR #121:" was the same reference restated, so it goes with
    # its introducing colon instead of rendering the label a second time.
    body = _render("completed", {"summary": _DOUBLED_LABEL_SUMMARY}).splitlines()[1]
    assert body == (
        "Opened [hermes-agent #121]"
        "(https://github.com/anombyte93/hermes-agent/pull/121)."
    )


def test_f2_pure_numbers_and_words_are_not_shas():
    # A bare hex token is dropped only when it mixes hex letters and digits;
    # a count ("10000000") and a word ("deadbeef") are not commits and stay.
    assert _render("completed", {"summary": "Moved 10000000 rows"}).splitlines()[1] == (
        "Moved 10000000 rows"
    )
    assert _render("completed", {"summary": "deadbeef module"}).splitlines()[1] == (
        "deadbeef module"
    )


def test_f2_sha_intro_words_still_drop_the_sha():
    # "head" / "at" / "@" / "commit" only ever introduce a SHA, so the token
    # after them drops even when it is pure letters or pure digits.
    summary = (
        "Re-verified at head 1111111 and pushed commit aaaaaaa "
        "(head 30e81b5c681fc31ab342ae32910a592992cb5f38)."
    )
    assert _render("completed", {"summary": summary}).splitlines()[1] == "Re-verified and pushed."


def test_f3_no_dangling_connectors_or_emptied_parens():
    # Paths are replaced by their file names, so connectors and
    # parentheticals keep their grammar. SHA removal can still empty a
    # parenthetical ("(head <sha>)"), and a sentence whose whole content
    # was removed machinery carries no plain-words information, so it is
    # dropped rather than padded with a synthesised file count.
    body = _render(
        "completed", {"summary": "Wrote /home/x/foo.py and ~/x/y.md (see gateway/a.py). Done."}
    ).splitlines()[1]
    assert body == "Wrote `foo.py` and `y.md` (see `a.py`). Done."


def test_f4_paren_with_nothing_removed_stays():
    # Known-red on f84e7ffd: a parenthetical is dropped only when the
    # path/SHA removal emptied it. "(see notes)" lost nothing, so it stays
    # even though it starts with "see"; same for "in"/"on"/"at" asides.
    body = _render("completed", {"summary": "Fixed 2 bugs (see notes). Shipped."}).splitlines()[1]
    assert body == "Fixed 2 bugs (see notes). Shipped."
    body = _render("completed", {"summary": "Approved it (in draft)."}).splitlines()[1]
    assert body == "Approved it (in draft)."
    body = _render("completed", {"summary": "Merged (on request)."}).splitlines()[1]
    assert body == "Merged (on request)."


# ------------------- #122 re-verify findings (t_78a6b5d2 FAIL samples)


def test_directory_path_with_trailing_slash_renders_once():
    # Known-red on c08baeba: a directory path ending in "/" matched
    # without its trailing slash, so the leftover "/" rendered after the
    # backticks ("logs are in `gateway/`/"). The slash belongs to the
    # path and renders once inside it.
    summary = (
        "Rebased onto origin/main at 9e8d7c6b and pushed; logs are in "
        "~/.hermes/logs/gateway/ and the report is /tmp/verify-report.md."
    )
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == (
        "Rebased onto origin/main and pushed; logs are in `gateway/` "
        "and the report is `verify-report.md`."
    )


def test_path_containing_a_sha_keeps_its_name():
    # Known-red on c08baeba: the SHA pass ran before the path pass, so a
    # hash inside a release-dir name was holed out ("`hermes-agent-/`")
    # and the PR-link pass left "()" plus the owner prefix behind. Paths
    # are replaced first, a hash inside a name is shortened to 8 chars,
    # and an owner-qualified reference links with the owner dropped.
    summary = (
        "Merged anombyte93/hermes-agent PR #120 "
        "(https://github.com/anombyte93/hermes-agent/pull/120) into "
        "feat/110-112-on-deployed; the release dir "
        "~/.hermes/releases/hermes-agent-07958b78faf05586e22117523fa8ae002a569de5/ "
        "is ready."
    )
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == (
        "Merged [hermes-agent #120](https://github.com/anombyte93/hermes-agent/pull/120) "
        "into feat/110-112-on-deployed; the release dir "
        "`hermes-agent-07958b78/` is ready."
    )


#          regression: the verifier's three OK samples stay OK (1, 3, 4)


def test_pr_link_then_path_still_renders_clean():
    # Verifier sample 1 (t_78a6b5d2): a PR URL, a "(head <sha>)" aside
    # and a long path in one summary.
    summary = (
        "Opened https://github.com/anombyte93/moll-e/pull/307 "
        "(head 4f2a1b3c5d7e9f0a1b2c3d4e5f6a7b8c9d0e4a3f) after editing "
        "/home/anombyte/atlas/worktrees/moll-e/src/relay/gate.ts."
    )
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == (
        "Opened [moll-e #307](https://github.com/anombyte93/moll-e/pull/307) "
        "after editing `gate.ts`."
    )


def test_exact_head_sha_and_relative_test_path_still_render_clean():
    # Verifier sample 3 (t_78a6b5d2): "at exact head <sha>" drops with
    # its scaffold; a repo-relative test path renders by file name.
    summary = (
        "PASS on PR #121 at exact head "
        "6e541234567890abcdef1234567890abcdefbd46: tests in "
        "tests/gateway/test_kanban_discord_notice.py pass (43 passed)."
    )
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == (
        "PASS on PR #121: tests in `test_kanban_discord_notice.py` pass "
        "(43 passed)."
    )


def test_two_paths_and_a_commit_sha_paren_still_render_clean():
    # Verifier sample 4 (t_78a6b5d2): two paths plus a "(commit <sha>)"
    # aside that the SHA removal empties.
    summary = (
        "Updated the config at ~/.config/atlas-vault/settings.toml and "
        "restarted nothing; see /home/anombyte/notes/2026-10-22.md for "
        "the diff (commit a1b2c3d4e5f)."
    )
    body = _render("completed", {"summary": summary}).splitlines()[1]
    assert body == (
        "Updated the config at `settings.toml` and restarted nothing; "
        "see `2026-10-22.md` for the diff."
    )

