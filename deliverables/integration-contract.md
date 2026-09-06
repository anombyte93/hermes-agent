# Codex thread continuity — integration contract

For the service adapter worker (t_4b6c3baa). Stable as of commit on branch
`hermes-astra-continuity-20260906`. Everything below is code that exists and
passes tests in this worktree; nothing here is aspirational.

Base: fork/main `a47f117ed0`.

## What it gives you

The same Hermes session id comes back to the **same codex thread** across
process restarts. A different session id gets a fresh thread. An unusable
saved thread is an **explicit error**, never a silent new conversation.

## Modules

- `agent/transports/codex_thread_continuity.py` — persistence + ownership.
- `agent/transports/codex_app_server_session.py` — resume-first
  `ensure_started()`, ownership release on `close()`.
- `agent/codex_runtime.py` — already wires both for any AIAgent with a session
  DB. **If your service uses `run_codex_app_server_turn`, you get continuity
  with no code change.**

## Minimal API (direct use, if you don't go through AIAgent)

```python
from agent.transports.codex_app_server_session import CodexAppServerSession
from agent.transports.codex_thread_continuity import (
    CodexThreadContinuity, SessionModelConfigThreadStore,
    CodexThreadContinuityError,          # base of every refusal
    CodexThreadResumeError,              # codex refused the saved id
    CodexThreadOwnershipError,           # another LIVE process owns it
    CodexThreadIdentityMismatch,         # different cwd / permission profile
    CodexThreadPersistenceError,         # store unreadable/unwritable
)

continuity = CodexThreadContinuity(
    SessionModelConfigThreadStore(session_db, session_id),
    cwd=cwd,                          # must match across restarts
    permission_profile=profile,       # must match across restarts
    model=model,                      # recorded, not enforced
)

session = CodexAppServerSession(
    cwd=cwd,
    permission_profile=profile,
    continuity=continuity,            # None → legacy fresh-thread-per-process
    model=model,
    approval_callback=...,            # unchanged
    on_event=...,                     # unchanged
)
```

Or, from an agent object that has `_session_db` and `session_id`:

```python
from agent.transports.codex_thread_continuity import build_session_continuity
continuity = build_session_continuity(
    agent, cwd=cwd, permission_profile=profile, model=model
)   # returns None for ephemeral agents — that is correct, not an error
```

### Constructor params added to `CodexAppServerSession`

| param | type | meaning |
|---|---|---|
| `continuity` | `CodexThreadContinuity \| None` | `None` keeps today's behaviour exactly. |
| `model` | `str` | Recorded into the durable record; also passed on resume-claim. |

### New surface

- `session.ensure_started()` — now resume-first. Raises `CodexThreadContinuityError` subclasses.
- `session.resumed` → `bool` — True when it attached to a pre-existing thread. **Use this to decide whether to replay your own history to the user; do not infer it from the thread id.**
- `session.close()` — releases ownership (keeps the thread id).
- `session.run_turn(...)` — a refusal comes back as
  `TurnResult.error` starting with `"Codex conversation continuity refused:"`
  and `TurnResult.should_retire = True`. It does **not** raise.

## Where the state lives

Session row `model_config` JSON, key `codex_thread`, via the existing
`patch_session_model_config` atomic merge (no schema change, lineage markers
preserved). Record fields: `thread_id, cwd, model, permission_profile,
owner_pid, owner_boot_id, owner_host_id, claimed_at, released_at, version, note`.

The reservation lock is a separate file:
`<dir-of-state.db>/codex-threads/<session_id>.lock`.

## Verified, not assumed

- Live proof against real codex-cli 0.153.4, run twice
  (`evidence/live-probe-run1-preserved.json`, `evidence/live-probe-run2-hostid.json`,
  both verdict PASS): a nonce stored in process 1 was recalled in process 2 on
  the exact same thread id; a separate session answered `NO_TOKEN_KNOWN`; a
  fabricated id was refused by codex itself
  (`-32600: no rollout found for thread id ...`).
- Six concurrent OS processes racing one session yield exactly one winner;
  with the reservation removed, all six win (falsification recorded).
- The same-host-reboot resume fails under the old boot-only comparison
  (falsification recorded), so that control is known to be load-bearing.

## Rules you must design around

1. **cwd and permission_profile must be stable per session.** Changing either
   between restarts is refused with `CodexThreadIdentityMismatch` — by design,
   because the thread's sandbox roots would no longer match.
2. **The reservation is taken on `ensure_started()` and held until `close()`.**
   `CodexThreadContinuity.load_resumable()` takes an exclusive non-blocking
   `flock` on `<dir-of-state.db>/codex-threads/<session_id>.lock` *before* it
   reads the record, so two processes racing to start the same session cannot
   both conclude "no owner, start fresh". The loser gets
   `CodexThreadOwnershipError` before any codex RPC. **Your service must call
   `session.close()` on shutdown** (the OS drops the lock on process exit, so a
   crash is also safe, but a long-lived process that leaks sessions will lock
   itself out).
3. **One owner at a time; host and boot are different questions.**
   - Same host + same boot + live pid → `CodexThreadOwnershipError`.
   - Same host + **different boot** → legitimate takeover, exact thread
     resumes. A cold reboot of the Evo host provably killed the predecessor,
     so the service reaches its own conversation. The takeover is written into
     the record's `note`.
   - Same host + dead pid (crash) → legitimate takeover.
   - **Different host**, no explicit release → always refused. A foreign pid
     cannot be probed. Ways through: an explicit release written by that owner,
     or an operator `invalidate()`.
4. **Refusal is not permission to start fresh.** If you decide a fresh thread is
   the right recovery, call `continuity.invalidate(reason)` explicitly and record
   that continuity break in your own job log. Never do it implicitly.
5. **A thread that cannot be persisted aborts the turn.** If the store write
   fails on a fresh start, `ensure_started()` raises and `run_turn` returns the
   refusal *without sending the user's message*. This is deliberate: a thread we
   could not record is one the next restart loses, and the user must not build a
   conversation on it believing it is durable.
6. **Only `SessionModelConfigThreadStore` is production-safe.** A custom store
   without a `lock_path()` method gets record-level checks only — it cannot stop
   two processes that both start before either saves. That case logs
   "durable exclusive thread ownership is UNSUPPORTED". Do not ship one.
7. **Compaction and event projection are untouched** — codex still owns
   compaction; `CodexEventProjector` is unchanged.

## Named limitations (carried forward, not fixed here)

- `_coerce_turn_input_text` drops image content to `"[image attached]"`.
  Attachments need separate work in the service layer.
- `thread/start` still sends `cwd` only (no `permissions`) — unchanged from
  base; codex's own config.toml is the policy gate.
- `run_turn` accepts no per-turn model argument; the model is recorded for
  provenance, not enforced on codex.
