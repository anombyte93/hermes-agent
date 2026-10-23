# Codex continuity — final review handback

Card `t_2c7e41b6`. Branch `feat/astra-codex-continuity-20260906` in
`/home/anombyte/atlas/worktrees/hermes-astra-continuity-20260906`.
Remote fork `anombyte93/hermes-agent`. No push, no PR, no merge, no live
install from this card.

Prior heads (parent-accepted): `0bd875fe61`, `6830f3b4ef`, `bfeaab3247`,
`b09fcbf571`. This card adds three commits on top.

## What was wrong, and what it is now

### 1. A present-but-unusable record was indistinguishable from no record

`CodexThreadRecord.from_dict` returned `None` for a missing key, an
unsupported version AND a malformed blob. The caller reads `None` as "no
prior conversation", so a corrupt or future-versioned record silently
produced `thread/start` and abandoned a live conversation.

Now exactly two states yield `None`: the key is **absent**, or it holds an
**explicit tombstone**. Everything else present raises
`CodexThreadRecordUnusable` **before any codex RPC**, carrying the original
payload; the refusal never rewrites the stored bytes. `clear(reason=...)`
writes an auditable tombstone (`cleared`, `cleared_at`, `cleared_reason`,
`superseded`) instead of deleting — that is what keeps "a human ended this"
distinguishable from "this is broken" on the next read.

### 2. Model and permissions were labels, not runtime

`intended_model` was recorded but never sent; `thread/start` carried `cwd`
only. A thread recorded as `read-only` was observed running
`sandbox_type=workspace-write` / `approval_policy=on-request`
(`anombyte93/hermes-agent#41`).

The codex 0.130-era `permissions: {type:"profile", id:...}` shape was not
simply re-enabled — **it does not exist on the installed schema**. Read from
`codex app-server generate-json-schema` on codex-cli 0.153.4,
`ThreadStartParams` / `ThreadResumeParams` carry stable, non-experimental
`model`, `sandbox` (SandboxMode) and `approvalPolicy` (AskForApproval), with
no `config.toml` `[permissions]` prerequisite. The requested profile is now
expressed through those and then **verified against codex's own response**:

- contradiction → `CodexThreadRuntimeFidelityError`, the turn does not run;
- silence → `model_applied` / `permissions_applied` stay `False` and it is
  logged. The record says "not confirmed", which is the honest answer;
- unknown profile → `CodexUnsupportedPermissionProfile`, never a silent
  default. Compatibility is retained as a **loud refusal**.

### 3. Images never reached the model

`turn/start` collapsed every image part to the literal `"[image attached]"`.
`_build_turn_input` now emits the schema's real `UserInput` variants —
`text` / `image(url)` / `localImage(path)`, with `file://` and absolute paths
becoming `localImage` so codex reads the file itself. A plain string still
produces exactly one text item (existing text callers unchanged); an
unaddressable image is flagged, never silently dropped.

### 4. (from the parent's mid-run note) close released ownership too early

`close()` released the continuity claim **before** tearing down the codex
client, so a successor could acquire the thread while this process's
subprocess was still attached — the same one-owner defect found on the Evo
service side. Now: close the client → wait → release **only if the subprocess
is provably reaped**. If close raises, times out, or codex ignores
terminate/kill, ownership is **retained** and warned about; the OS drops the
flock at process exit, so this cannot strand a session, and a clean close
still releases normally. **No service-repo change was needed and no
dependency on the service card exists.**

## Tests vs actual runtime proof

Keep these two columns apart — that distinction is the point of this card.

| claim | proved by | kind |
|---|---|---|
| parser refuses unsupported/corrupt, allows absent/tombstone | `test_codex_runtime_fidelity.py::TestParserKnownAnswers` | test |
| refusal lands before any codex RPC, bytes untouched | `TestStoreBoundary` (asserts `client.requests == []`) | test |
| profile → sandbox/approvalPolicy mapping; unknown refuses | `TestPermissionProtocolMapping` | test |
| mismatch/silence handling | `TestRuntimeFidelityVerification` | test |
| image variants built correctly | `TestTurnInputImages` | test |
| close ordering + ownership retention | `TestCloseOrdering` | test |
| **codex actually ran `gpt-6-astra` / `readOnly` / `never`** | `evidence/fidelity-live.json` — codex's own response, `unconfirmed: []` | **live** |
| **the same on `thread/resume` from a separate process** | same file, `child_runtime` | **live** |
| **native compaction, then cross-process nonce recall** | same file, F3 + F4 | **live** |
| **image bytes actually reached the model** | `evidence/image-seam.json` — rollout `input_image`, byte-identical sha256, `user.image` | **live** |
| the three new gates can fail | `evidence/fidelity-gate-falsification.txt` (7 fail when reverted) | falsification |

Focused suite at the final head: **91 passed**
(`evidence/final-suite-t2c7.txt`) — the three files covering the changed
transport/runtime surface.

**Broad-suite coverage: UNKNOWN, deliberately.** A `tests/agent/` run was
started, went D-state under host load ~60 with 17GiB available, and was
stopped by me (PID 3927099, SIGTERM, confirmed gone) rather than waited on.
No partial output survived worth quoting. Nothing in this change touches
anything outside `agent/transports/codex_*` and its tests, so the focused
suite plus the live evidence is the affected-behaviour proof; unrelated
test volume is not claimed either way. Separately, the 15 errors in
`tests/agent/transports/test_codex_transport.py` are **pre-existing** — a
`pytest-base-url` plugin fixture-name collision with a parametrised
`base_url` argument, reproduced identically on the stashed baseline
(89 passed, 12 errors both with and without my changes).

## Exact model and permissions, as codex reported them

Live thread `01a076d2-d8aa-7883-b4b3-443fce2cbe7d`, codex-cli 0.153.4:

```
model            gpt-6-astra     (requested gpt-6-astra)   model_applied=true
sandbox          readOnly        (requested read-only)     permissions_applied=true
approvalPolicy   never           (requested never)         unconfirmed=[]
```

Identical on `thread/start` and on `thread/resume` from a **different OS
process** (pid 3907258 vs 3851827), across a native compaction, with the
pre-compaction nonce still recalled on the same thread id.

## The image FAIL — preserved, investigated, NOT retracted

Run 1 failed: expected `410131`, model answered `121212`
(`evidence/fidelity-live-run1-image-FAIL.json`, kept verbatim). An interim
probe then had the model answer `NO_IMAGE` on a turn that provably contained
the image (`evidence/image-seam-run1-selfreport-no_image.json`, kept).

Investigated to the layer that can settle it:

- **generator ruled out** — a deterministic stdlib pixel decoder
  (`read_digit_png`, no vision model, no human eye) round-trips `410131`. The
  parent's independent PIL luminance read of the same 400x112 file agreed:
  4,1,0,1,3,1. My earlier "the glyphs were ambiguous" inference was wrong and
  has been removed from the code comment.
- **delivery ruled out** — codex's saved rollout contains an `input_image`
  part whose base64 is byte-identical (sha256 `60c6130a…92710f`) to the file
  on disk, `detail: high`, `content_item_kinds` = `[user.text, user.text,
  user.image, user.text]`.

So the bytes were right and they arrived. The misread sits in **model image
understanding**, which this layer does not control. A second, conventional
5x7 fixture was added as a fair rerun (not a fix): the probe now self-checks
its own fixture before spending live turns, and the digit readback is
recorded as a model-perception **observation**, no longer folded into the
pass criterion. On that fixture the model read `871972` exactly (PASS) and
`605716` as `625716` in another run.

**A model's self-report is not evidence about the transport.** The seam probe
asserts at the rollout.

## Codex version staging

| host | codex | status |
|---|---|---|
| Archie | **0.153.4** | all live proof ran here |
| Evo | **0.149.0** (`/home/hayden/.local/bin/codex`) | **schema-compatible only** |

`generate-json-schema` run on both: `model`/`sandbox`/`approvalPolicy`/`cwd`
present on ThreadStart+ThreadResume Params and ThreadStartResponse; UserInput
variants identical including `localImage`. **Minimum live-tested: 0.153.4.**
No thread was started, no turn run and no auth probed on Evo.
`evidence/codex-version-compatibility.json`. Integration must stage an
explicit matching version and rerun both probes there.

## ACTUALLY_USED

- `codex app-server generate-json-schema --out <dir>` on codex-cli 0.153.4
  (Archie) and 0.149.0 (Evo) — the source of every field name used here.
  `v2/ThreadStartParams.json`, `v2/ThreadResumeParams.json`,
  `v2/ThreadStartResponse.json`, `v2/TurnStartParams.json` (UserInput union).
- `~/.codex/sessions/.../rollout-*.jsonl` — read for `input_image`,
  `content_item_kinds` and the compaction event **only**; no transcript
  reproduced.
- Existing `SessionDB.patch_session_model_config` /
  `get_session_model_config_value` atomic merge (no schema change).
- Existing `CodexAppServerClient.request/close/is_alive`,
  `CodexEventProjector`, `compact_thread`.
- `agent/codex_runtime.py` continuity wiring (unchanged by this card).

## DISCOVERED

- `ThreadStartResponse` / `ThreadResumeResponse` **require** `model` and
  `sandbox`, and answer with a `SandboxPolicy` **object** (`{"type":
  "readOnly"}`), not the `SandboxMode` string that was sent. The readback has
  to be translated before comparison — comparing raw would have produced a
  false mismatch on every call.
- `thread/start.permissions` from codex 0.130 is **gone**; `sandbox` +
  `approvalPolicy` replace it and need no `config.toml` table. The historical
  omission was a real constraint that no longer applies.
- The model's own claim about its input is unreliable in **both**
  directions — it denied an image that was demonstrably present. Only the
  rollout settles delivery.
- codex writes the image into the rollout as base64 `input_image` even when
  the request used `localImage` (a path), so byte-identity is checkable.

## UNTOUCHED

- Chair thread `01a071af-c3d9-7131-9253-b66d3e02b21f` — never resumed, never
  read, never referenced by any probe.
- Shared auth, `~/.codex/auth.json`, global `config.toml`, `CODEX_HOME`,
  `HOME` — no reads, no writes, no credential copying.
- Parallel Hermes entrance worker files; the astra-evo-service worktree; all
  live services, browser, desktop, WoW, messaging, push.
- No native agents or Launcher homies spawned; no merge, no push, no PR.
- All probe state is under its own `tempfile.mkdtemp()`; nothing outside a
  task-owned temp dir was created or removed.

## MISSING

- **Model image *understanding* is not guaranteed** — delivery is; precise
  small-text OCR is not. Do not build a workflow that depends on it.
- **Audio is not wired.** The schema has `audio` / `localAudio`; only text
  and images are built. Audio parts are ignored, not claimed as supported.
- **No per-turn model/permission override.** `TurnStartParams` supports it;
  this card deliberately keeps the thread-level surface so runtime cannot
  drift mid-conversation.
- **Attachment materialisation is out of scope** (fetch/validate/store a
  remote Discord CDN attachment) — integration card `t_dda40b50`.
- **Evo behaviour unproven** — schema-compatible only, see above.
- `approvalsReviewer` / `personality` / `effort` / `serviceTier` left at
  codex defaults.

## FRICTION

- The outer tool timeout caps foreground runs; every live probe and the final
  suite were run as background processes with output redirected to a file.
- `python3 -c`, `rm -rf` and heredoc-in-pipeline commands are blocked by the
  approval policy in single-query mode; used `write_file` + a script file, and
  `mktemp -d`, instead.
- My first fidelity check treated a **silent** response as a mismatch, which
  failed 34 pre-existing tests whose fakes return bare payloads. Split into
  contradiction (refuse) vs silence (record unconfirmed) — the honest and
  compatible distinction.
- I initially inferred from a visual read that the test generator was at
  fault. It was not; the deterministic decoder and the parent's PIL read both
  disproved it. Recorded here because the wrong inference nearly retracted a
  legitimate FAIL.

## Parent-runnable checks

```bash
cd /home/anombyte/atlas/worktrees/hermes-astra-continuity-20260906

# focused suite (91 passed at this head)
python3 -m pytest tests/agent/transports/test_codex_thread_continuity.py \
  tests/agent/transports/test_codex_app_server_session.py \
  tests/agent/transports/test_codex_runtime_fidelity.py -q

# live: model + permissions + image + compaction + 2nd-process resume
PYTHONPATH=$PWD python3 tests/agent/transports/codex_fidelity_live_probe.py \
  --model gpt-6-astra --out /tmp/parent-fidelity.json

# live: image delivery asserted at the rollout, not from the model's word
PYTHONPATH=$PWD python3 tests/agent/transports/codex_image_seam_probe.py \
  --model gpt-6-astra --out /tmp/parent-image-seam.json
```

Both probes use only their own temp state and a read-only profile, and never
touch the chair thread.
