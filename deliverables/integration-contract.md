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
    model=model,                      # requested; APPLIED+VERIFIED, see below
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

Additional refusals to catch:

```python
from agent.transports.codex_thread_continuity import (
    CodexThreadRecordUnusable,        # a record IS present but unreadable
    CodexThreadRuntimeFidelityError,  # codex is not running what we asked
)
from agent.transports.codex_app_server_session import (
    CodexUnsupportedPermissionProfile,  # profile has no protocol expression
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
| `model` | `str` | **Sent on the actual `thread/start` / `thread/resume` RPC** and verified against codex's response. Empty = codex's configured default. |
| `permission_profile` | `str` | **Applied via the real protocol fields** (see "Model and permissions" below). Empty = no override. |

### New surface

- `session.ensure_started()` — now resume-first. Raises `CodexThreadContinuityError` subclasses.
- `session.resumed` → `bool` — True when it attached to a pre-existing thread. **Use this to decide whether to replay your own history to the user; do not infer it from the thread id.**
- `session.runtime` → `dict` — what codex ITSELF reported for this thread:
  `{model, model_applied, sandbox, approval_policy, permissions_applied, unconfirmed}`.
  Empty before `ensure_started()`. **`*_applied` False means NOT PROVEN — never
  render it as a guarantee.**
- `session.close()` — closes codex FIRST, releases ownership only once the
  subprocess is reaped (see rule 8).
- `session.run_turn(...)` — a refusal comes back as
  `TurnResult.error` starting with `"Codex conversation continuity refused:"`
  and `TurnResult.should_retire = True`. It does **not** raise.
- `store.clear(reason=...)` — writes an auditable tombstone, not a delete.
- `store.load_raw()` — the stored payload verbatim, for evidence on a refusal.

## Model and permissions — applied, then proven

Read from `codex app-server generate-json-schema` on the installed
**codex-cli 0.153.4**, not from memory. `ThreadStartParams` and
`ThreadResumeParams` both carry:

| field | type | values |
|---|---|---|
| `model` | string | e.g. `gpt-6-astra` |
| `sandbox` | `SandboxMode` | `read-only` \| `workspace-write` \| `danger-full-access` |
| `approvalPolicy` | `AskForApproval` | `untrusted` \| `on-request` \| `never` |

These are stable and non-experimental on this build, and need no
`[permissions]` table in `config.toml`. The codex 0.130-era
`permissions: {type: "profile", id: ...}` shape does **not** exist here; the
historical omission was a real compatibility decision, and this replaces it
rather than simply deleting the restriction.

Profile → protocol:

| Hermes profile | `sandbox` | `approvalPolicy` |
|---|---|---|
| `read-only` | `read-only` | `never` |
| `read-only-with-approval` | `read-only` | `on-request` |
| `workspace-write` | `workspace-write` | `on-request` |
| `full-access` | `danger-full-access` | `never` |
| *(empty)* | — not sent — | — not sent — |
| *(anything else)* | **`CodexUnsupportedPermissionProfile`** | refusal, not a default |

The response is then checked against the request
(`ThreadStartResponse.model` / `.sandbox` / `.approvalPolicy`, all required
fields on this schema):

- **Contradiction** → `CodexThreadRuntimeFidelityError`, the turn does not run.
  This is the `anombyte93/hermes-agent#41` case: a thread recorded as
  `read-only` observed running `sandbox_type=workspace-write` /
  `approval_policy=on-request`.
- **Silence** (field absent) → logged, and `model_applied` /
  `permissions_applied` stay `False`. The record then says "not confirmed",
  which is the honest answer. **A metadata label is never proof.**

## Image input

`turn/start` now sends the schema's real `UserInput` variants instead of
collapsing images to the string `"[image attached]"`:

| Hermes content part | sent as |
|---|---|
| `str`, `{"type":"text"}` | `{"type":"text","text":...}` |
| `http(s)://` or `data:` image | `{"type":"image","url":...}` |
| `file://...` or absolute path | `{"type":"localImage","path":...}` — codex reads the file itself |
| image with no usable URL | a text item saying so — flagged, never silently dropped |

A plain string still produces exactly one text item, so existing text callers
are byte-identical. `_coerce_turn_input_text` is retained for callers that
genuinely want a flat string.

## Where the state lives

Session row `model_config` JSON, key `codex_thread`, via the existing
`patch_session_model_config` atomic merge (no schema change, lineage markers
preserved). Record fields: `thread_id, cwd, model, permission_profile,
owner_pid, owner_boot_id, owner_host_id, claimed_at, released_at, version, note`
plus the runtime-fidelity fields `model_applied, permissions_applied, sandbox,
approval_policy` (all populated **only** from codex's own response).

The reservation lock is a separate file:
`<dir-of-state.db>/codex-threads/<session_id>.lock`.

### Three distinct states of that key

| stored value | meaning | behaviour |
|---|---|---|
| key absent | no prior conversation | `thread/start` (correct) |
| `{"cleared": true, "cleared_at":…, "cleared_reason":…, "superseded":…}` | **explicitly** abandoned by an operator | `thread/start` (correct) |
| a valid v1 record | a conversation exists | `thread/resume` with the exact id |
| anything else present (bad version, malformed, non-object, blank id) | **unknown** | `CodexThreadRecordUnusable` **before any codex RPC**; the stored bytes are left untouched |

The last row is the repair. Previously all four non-record cases returned
`None`, so a corrupt or future-versioned record was indistinguishable from no
conversation and the runtime silently started a new thread over a live one.
`clear()` writes a tombstone rather than deleting precisely so that "a human
ended this" stays distinguishable from "this is broken" on the next read; the
superseded payload is carried inside the tombstone as recoverable evidence.

## Codex version staging — REQUIRED, not optional

| host | codex | status |
|---|---|---|
| Archie | **0.153.4** | every live probe in this card ran here |
| Evo | **0.149.0** (`/home/hayden/.local/bin/codex`) | **schema-compatible only — no live turn run** |

`generate-json-schema` was run on **both**: `model` / `sandbox` /
`approvalPolicy` / `cwd` are present on `ThreadStartParams`,
`ThreadResumeParams` and `ThreadStartResponse` on each, and the `UserInput`
variants are identical (including `localImage`). **Minimum live-tested version
is 0.153.4.** A schema match is necessary, not sufficient: no thread was
started and no turn was run on Evo from this card, and its auth was not
probed. Integration must stage an explicit matching version rather than
inherit whatever is installed, and rerun both live probes there.
Full detail: `evidence/codex-version-compatibility.json`.

## Verified, not assumed

- Live proof against real codex-cli 0.153.4, run twice
  (`evidence/live-probe-run1-preserved.json`, `evidence/live-probe-run2-hostid.json`,
  both verdict PASS): a nonce stored in process 1 was recalled in process 2 on
  the exact same thread id; a separate session answered `NO_TOKEN_KNOWN`; a
  fabricated id was refused by codex itself
  (`-32600: no rollout found for thread id ...`).
- **Runtime fidelity, live** (`evidence/fidelity-live.json`, verdict PASS,
  thread `01a076d2-d8aa-7883-b4b3-443fce2cbe7d`): codex's own response
  reported `model=gpt-6-astra`, `sandbox=readOnly`, `approvalPolicy=never`
  with no unconfirmed fields — the exact values requested, on both
  `thread/start` and, from a **separate python process**, `thread/resume`.
  Native `thread/compact/start` ran between them, and the second process
  still recalled the pre-compaction nonce on the same thread id.
- **Image delivery, at the rollout layer** (`evidence/image-seam.json`,
  verdict `IMAGE_DELIVERED_TO_MODEL`): codex's saved rollout contains an
  `input_image` part whose base64 is **byte-identical (sha256)** to the file
  on disk, `detail: high`, and codex's own `content_item_kinds` lists
  `user.image`. The request carried a `localImage` item.
- Six concurrent OS processes racing one session yield exactly one winner;
  with the reservation removed, all six win (falsification recorded).
- The same-host-reboot resume fails under the old boot-only comparison
  (falsification recorded), so that control is known to be load-bearing.
- **The three new gates have been seen to fail** (
  `evidence/fidelity-gate-falsification.txt`): with the parser refusal, the
  fidelity check and the close-ordering rule each reverted, 7 tests fail —
  the version refusal, the before-RPC and evidence-preservation checks, both
  runtime mismatches, and both ownership-retention checks.

### A model's self-report is NOT evidence about the transport

Measured on this build: asked "was an image attached?", the model answered
`NO_IMAGE` on a turn whose rollout provably contained the image
(`evidence/image-seam-run1-selfreport-no_image.json`). An earlier run also
misread the digits (`evidence/fidelity-live-run1-image-FAIL.json`) — that
failure is preserved and is **not** retracted. Both were investigated to the
rollout, where the bytes were byte-identical and correctly tagged. Assert
delivery at the rollout; treat what the model says about its own input as an
observation only.

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
7. **Native compaction is unchanged and still codex's.** `compact_thread()`
   drives `thread/compact/start`; `CodexEventProjector` is untouched. Compaction
   does not affect the record — the thread id and ownership survive it.
8. **Ownership outlives a *requested* close, not the process.** `close()` tears
   down the codex client and waits for the subprocess, then releases the claim
   **only if the subprocess is provably reaped**. If close raises, times out, or
   codex ignores terminate/kill, ownership is **retained** and a warning is
   logged: a successor must not acquire a thread whose previous owner may still
   be attached to it. The OS drops the flock at process exit, so this cannot
   strand the session permanently, and a clean close still releases normally.
   *(This is the same one-owner ordering defect the service worker found:
   releasing before the owned process is actually gone lets a second engine
   acquire while the first close is still blocked.)*
9. **Never read a label as runtime.** `record.model` / `record.permission_profile`
   are what was *requested*. Only `model_applied` / `permissions_applied` — set
   from codex's own response — mean it was confirmed. Render an unconfirmed
   value as UNKNOWN.

## Named limitations (honest, not fixed here)

- **`run_turn` still accepts no per-turn model/permission override.** The
  schema does support per-turn `model` / `sandboxPolicy` / `approvalPolicy` on
  `TurnStartParams`; this change deliberately keeps the thread-level surface
  only, so a session's runtime cannot drift mid-conversation. A service needing
  per-turn override must extend `run_turn` explicitly.
- **Model image *understanding* is not guaranteed.** Delivery is proven; the
  model reading the image correctly is not this layer's to promise. Measured
  on gpt-6-astra: 3x5 digit glyphs were misread outright (`410131` → `121212`);
  5x7 glyphs were read exactly right in one run and 5-of-6 in another. Do not
  build a workflow that depends on precise small-text OCR from an image.
- **Audio input is not wired.** The schema has `audio` / `localAudio`
  `UserInput` variants. Only text and images are built today; audio parts are
  ignored by `_build_turn_input` rather than being claimed as supported.
- **Attachment *materialisation* is out of scope here.** This layer carries a
  URL or a local path to codex. Fetching, validating and storing a remote
  attachment (Discord CDN, size/type limits) is the integration card's work
  (`t_dda40b50`); nothing here should be read as proof that arbitrary remote
  attachments are safe to pass.
- **`approvalsReviewer`, `personality`, `effort` and `serviceTier` are not
  set.** They exist on the schema and are left at codex's defaults.
- **Only codex 0.153.4 has live proof.** See the version-staging section.
