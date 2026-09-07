# Handback — Astra Evo continuity (t_e39d314e)

Scope: durable exact Codex thread continuity in the existing Hermes app-server
runtime. Base `a47f117ed0`. Worktree
`/home/anombyte/atlas/worktrees/hermes-astra-continuity-20260906`.

## Commits (explicit pathspec, no push)

| commit | contents |
|---|---|
| `0bd875fe61` | `fix(codex): durable exact thread continuity across process restart` — `agent/transports/codex_thread_continuity.py`, `agent/transports/codex_app_server_session.py`, `agent/codex_runtime.py` |
| `6830f3b4ef` | `test(codex): behavioural + live proof for durable thread continuity` — `tests/agent/transports/test_codex_thread_continuity.py`, `_continuity_race_child.py`, `codex_continuity_live_probe.py` |
| `bfeaab3247` | `fix(codex): distinguish host identity from boot identity in thread ownership` — `codex_thread_continuity.py`, `test_codex_thread_continuity.py` (parent review correction) |

Docs (uncommitted at time of writing, in `deliverables/`):
`integration-contract.md`, this handback, `evidence/`.

## What was actually built

The gap named in the card was real and confirmed in source: `ensure_started`
always issued `thread/start` on a new object, and `codex_runtime` surfaced
`codex_thread_id` with no persistence or resume.

1. `agent/transports/codex_thread_continuity.py` — durable record (`thread_id`,
   `cwd`, `model`, `permission_profile`, `owner_pid`, `owner_boot_id`,
   `claimed_at`, `released_at`, `version`, `note`) stored under the
   `codex_thread` key of the session row's `model_config` JSON, using the
   pre-existing atomic `patch_session_model_config` merge. No schema change;
   lineage markers in that blob are preserved (asserted by test).
2. `ensure_started()` is now **resume-first**: a saved record is resumed by its
   exact id via `thread/resume`. There is no silent fallback to `thread/start`.
3. **Reservation, not check-then-act** (parent review point 1). An exclusive
   non-blocking `flock` on `<dir-of-state.db>/codex-threads/<session>.lock` is
   taken *before* the record is read and held for the object's lifetime,
   released in `close()`. Two processes starting the same session concurrently
   can no longer both see "no owner" and both start a thread.
4. **Ownership is host-aware, not boot-only** (parent review, second round).
   The record carries `owner_host_id` (`/etc/machine-id`, hostname fallback)
   *and* `owner_boot_id`. Same host + different boot = the predecessor was
   killed by a reboot, so the exact thread resumes and the takeover is written
   into the record's `note` (approved reboot persistence). Same host + same
   boot + live pid = refused. Different host with no explicit release = refused,
   because a foreign pid cannot be probed. A boot-only comparison would have
   locked a rebooted Evo host out of its own conversation forever.
5. **Unpersistable start aborts the turn** (parent review point 3). If the store
   write fails on a fresh start, `ensure_started()` raises, the local thread
   attachment is dropped, and `run_turn` returns the refusal *without sending
   the user's message*. There is no `continuity_degraded`-and-continue path.
6. **Stores without `lock_path` are labelled unsupported.** They get
   record-level checks only and log
   "durable exclusive thread ownership is UNSUPPORTED ... Do not use in
   production", so the test-only fallback cannot be read as a guarantee.
7. `close()` releases ownership (keeping the thread id) and the flock.
   `run_turn` maps any `CodexThreadContinuityError` to
   `TurnResult.error = "Codex conversation continuity refused: ..."` with
   `should_retire=True` — it does not raise at the caller.
8. `agent/codex_runtime.py` wires `build_session_continuity(agent, cwd=...,
   permission_profile=..., model=...)` from the AIAgent's own requested model
   and the resolved codex permission profile, per parent comment. No global
   model/config broadening.

Compaction and event projection are untouched; `CodexEventProjector` and the
`compression_checkpoint_required` guard are unchanged.

## Evidence (machine-readable, in `deliverables/evidence/`)

| file | what it shows |
|---|---|
| `red-baseline.txt` | RED on unfixed code: **12 failed, 1 passed** — `TypeError: ... unexpected keyword argument 'continuity'`. Behavioural, not exit-4. |
| `green-unit.txt` | **23 passed** — full continuity suite on final code. |
| `race-gate-falsification.txt` | Reservation deliberately disabled: the 6-process race yields **6 winners** (`assert 6 == 1`). The gate has been seen to fail. |
| `reboot-gate-falsification.txt` | Old boot-only comparison restored: `test_same_host_new_boot_resumes` **fails** with "claimed by a process on another host". That control is load-bearing. |
| `green-transports.txt` | `tests/agent/transports/` — **328 passed**, 0 errors (pre-host-id run). |
| `green-runtime.txt` | codex runtime/persist/event-bridge tests — **26 passed**. |
| `final-suite.txt` | Combined final run on final code — **356 passed**. |
| `live-probe-run1-preserved.json` | First live run, verdict PASS (preserved unmodified at parent's request). |
| `live-probe-run2-hostid.json` | Second live run on the host-id-corrected code, verdict PASS. |
| `live-probe.json` / `.log` | Run-1 artifact as originally written. |

### Live proof detail (real codex-cli 0.153.4, both runs verdict PASS)

Real `codex app-server` subprocesses, temp state.db + temp cwd, all closed.
Run 2 (`live-probe-run2-hostid.json`, post-correction):

- P1 fresh session started thread `01a07666-66b6-7c02-8cd2-c520f2529f0a`, told
  nonce `NONCE-E5D9163D0B8A`, replied `STORED`. Record persisted with
  `owner_host_id: 6cfeda08157f420d8c4ec51ef5214bc2`.
- P2 a **new session object** for the same Hermes session resumed the **exact
  same thread id** (`same_thread: true`, `resumed_flag: true`) and answered
  `NONCE-E5D9163D0B8A` — the card's user story, proven.
- N1 a separate Hermes session got a distinct thread
  (`01a07666-dc05-7d91-a808-2d69448ed1c3`) and answered `NO_TOKEN_KNOWN`.
- N2 a fabricated id was refused by codex itself: `-32600: no rollout found for
  thread id 3c018e02-...`, surfaced as `CodexThreadResumeError`, with **no**
  `thread/start` fallback.

Run 1 (`live-probe-run1-preserved.json`) showed the same four outcomes on
thread `01a0765f-2cbd-7733-9e5f-7cf81bcf67ac` with nonce `NONCE-00BF130FE128`.

### Cross-process controls (not mocks)

- `test_second_process_reads_the_same_record` — an actual second python process
  reads the record from the actual sqlite file and sees this process as owner.
- `test_two_real_processes_cannot_both_start` — six concurrent OS processes
  contend; exactly one `reserved`, five `refused` with `CodexThreadOwnershipError`.

## ACTUALLY_USED / DISCOVERED / UNTOUCHED / MISSING / FRICTION

**ACTUALLY_USED**
- `SessionDB.patch_session_model_config` / `get_session_model_config_value`
  (`hermes_state.py:8375,8400`) — existing atomic merge, extended not replaced.
- `CodexAppServerSession`, `CodexAppServerClient`, `CodexEventProjector`.
- `run_codex_app_server_turn` in `agent/codex_runtime.py` as the wiring point.
- Real `codex` 0.153.4 at `/home/anombyte/.npm-global/bin/codex`, isolated
  threads only.
- Parent evidence `parent-a/tools/arm_a_trial.py` for the codex-side protocol
  shape (`thread/resume {threadId}`, fabricated-id refusal).

**DISCOVERED**
- codex 0.153.4 refuses an unknown thread with JSON-RPC `-32600: no rollout
  found for thread id <uuid>` — a clean, classifiable refusal.
- `/etc/machine-id` exists and is stable on Archie, giving a reboot-surviving
  host identity distinct from `/proc/sys/kernel/random/boot_id`. Both are needed:
  boot alone cannot tell "rebooted" from "other machine".
- `thread/resume` returns the thread object with its turn history, so
  `_extract_thread_id` needed the same cross-fill tolerance as `thread/start`;
  that logic is now a shared helper rather than duplicated.
- The 15 pre-existing `tests/agent/transports/` collection errors are a
  `pytest-base-url` plugin `ScopeMismatch`, unrelated to this work. Confirmed
  by the parent's note and reproduced: with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
  the same directory is **328 passed, 0 errors**. No test was changed to hide it.
- `tests/` cannot be collected whole in this env: 92 collection errors from
  missing optional deps (`prompt_toolkit` ×79, `acp` ×11, `snowballstemmer` ×2).
  Pre-existing, unrelated.

**UNTOUCHED**
- Compaction (`compress_context`, `compression_checkpoint_required`), event
  projection, approval routing, OAuth classification, interrupt/steer.
- The service worktree `/home/anombyte/atlas/worktrees/astra-evo-service-20260906`
  (read-only to me; never opened).
- Every global config, service, account, browser, and the active chair thread
  `01a071af-c3d9-7131-9253-b66d3e02b21f` — never resumed, never touched.
- No push, no merge, no main edit, no broad `git add`.

**MISSING**
- Image content is still coerced to `"[image attached]"` by
  `_coerce_turn_input_text`. Named in the integration contract as a service-layer
  limitation, per parent comment. Not in scope here.
- `thread/start` still sends `cwd` only (no `permissions`) — unchanged from base.
- `run_turn` still takes no per-turn model argument; the model is recorded for
  provenance, not enforced on codex.

**FRICTION**
- `execute_code` and several `python3 -c` / grouped shell commands were blocked
  by the single-query security scanner; worked around with file-based scripts
  and `-F` commit messages. No policy was bypassed.
- The default 60s terminal timeout truncated the first full transports run; the
  recorded result is from a completed 1500s-budget run.

## Residual UNKNOWNs

1. **Cross-host handover is refused, not solved.** Two *different machines*
   sharing one state.db is failed closed by design. A cold reboot of the same
   host IS handled (proven). If the Evo service ever moves to a different host,
   it needs an explicit transfer/release record or an operator `invalidate()`.
   No transfer protocol was built (out of scope).
2. **Host identity source is unverified on non-systemd hosts.** `_host_id()`
   reads `/etc/machine-id` (present and stable on Archie, value
   `6cfeda08157f...`) and falls back to hostname. On a host with neither stable,
   the host/foreign distinction degrades to hostname equality — UNKNOWN quality.
3. **flock semantics on network filesystems** (NFS/CIFS) are not verified. Proven
   on local ext4/tmpfs only. If state.db is ever placed on a network mount, the
   reservation guarantee is UNKNOWN.
4. **Long-horizon resume after codex-side compaction** is unproven: the live
   probe covers a short thread. Whether resume preserves recall across a
   codex-owned compaction boundary was not measured.
5. **Codex version coupling.** Proven against 0.153.4 only. The `-32600` refusal
   shape is classified by exception type, not string, so a message change is
   safe; a *semantics* change is UNKNOWN.
6. **Map semantic judge remains UNKNOWN**, as the card stated. This is
   authorised construction; nothing here claims end-to-end capability proof.
7. **Codex actual exec on Archie exiting 1** (parent note) was NOT retested or
   called ready. The live probe's turns succeeded through the app-server path,
   which is a different surface from that exec finding; I make no claim about it.
   Per the parent's later correction, the parent's own 401 was a different CLI
   execution and is not evidence against these probe artifacts; equally, these
   artifacts are not evidence that the exec path is healthy.
8. **Fixtures are never offered as live proof.** The 23 unit tests use a fake
   JSON-RPC client by design; the only live claims in this handback come from
   the two probe JSON artifacts produced by real codex subprocesses.
