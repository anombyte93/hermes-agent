# Cutover manifest — Hermes bridge side

**This card does not perform cutover.** It builds and verifies the client
bridge. This manifest states exactly what the parent must satisfy before the
shared conversation becomes Hayden's live Astra, and exactly what a rollback
preserves. Nothing here was executed; every item is a precondition, not a
report.

Not touched by this card, by construction: thread
`01a071af-c3d9-7131-9253-b66d3e02b21f`, the old Archie relays, any live
service, any Discord send, any global account state. The bridge writes no
files outside the worktree and installs nothing.

## 1. Old-owner release preconditions

Before the shared service may take ownership:

1. **The old owner is identified, not assumed.** Its PID, its host, and the
   process that holds the Discord DM must be read from live state, not from a
   recorded parent.
2. **The old owner is quiesced with no turn in flight.** A turn interrupted
   mid-flight is `uncertain` by the service's own contract — it must be
   resolved (`resolve_uncertain` → `rerun` or `abandon`) by a human decision,
   never automatically.
3. **The old owner's outbox is drained or explicitly declared undelivered.**
   `outbox` must be empty, or each pending chunk recorded by id in the cutover
   log with its state. An `uncertain` chunk is a human decision.
4. **The old owner is stopped before the new one starts.** Two live owners on
   one conversation is the failure this whole design exists to prevent. The
   service's lease refuses to evict a *live* holder by timeout, so an
   un-stopped old owner will block the new one rather than duplicate it —
   that refusal is the safety net, not the plan.
5. **The Discord token is provisioned to exactly one holder.** The service
   resolves it per API call from `~/.config/astra-evo/secrets.env`; the old
   owner must no longer hold a usable copy.

## 2. Transcript migration with loss-free tail capture

The tail — messages arriving *during* the switch — is where a migration loses
data. Ordering:

1. **Freeze the read boundary, not the writes.** Record the last message id and
   `seq` the old owner processed. Do not stop Discord from delivering.
2. **Import history up to the boundary** into the service's conversation store.
   `record_inbound` archives the original before processing and
   `UNIQUE(source, source_native_id)` makes the import idempotent, so a partial
   import can be re-run without duplication.
3. **Catch up the tail from the source, not from the old process.** The service
   has a bounded `catchup` that pages from Discord and reports `truncated`
   honestly. Run it after the boundary import and confirm `truncated: false`.
   A truncated catch-up is a stop condition, not a warning.
4. **Verify the join point.** The last pre-boundary message and the first
   post-boundary message must both be present exactly once, in `seq` order.
5. **Only then start the service unit** (`systemctl --user start
   astra-evo-service`) — that command is the ownership-taking act.

## 3. Exclusive ownership handover

* One `owner_lease` row, held by the service, with a live pid/boot-id.
* `status().owner.held_by_this_process` is `true` in the service process and
  `false` in every client — including every Hermes entrance. A client that
  reported `true` would be a bug, not a configuration choice.
* The Hermes bridge never calls `run_turn`. Confirm by inspection of
  `tui_gateway/shared_conversation.py` (no `run_turn` call site exists) and by
  the test that asserts `run_turn_calls == 0` after a full submit/reply cycle.
* No Hermes profile other than the shared one may be pointed at the
  conversation: an ordinary profile builds its own agent and would be a second
  runner.

## 4. Rollback that preserves post-cutover originals

* `deployment/astra-evo/uninstall.sh` (service repo) stops and disables the
  unit and removes deployed code. It **never** deletes the conversation
  database or the archive — messages received after cutover survive a
  rollback. That is the property that makes the cutover reversible.
* On the Hermes side, `docs/scripts/astra-shared.sh rollback` removes only
  `~/.hermes/profiles/astra-shared`. It touches no service state, no
  transcript, and no other profile.
* A rollback therefore leaves the post-cutover conversation intact and
  re-importable. Any decision to discard it is explicit and human.

## 5. Cross-host exact-thread migration — prove it on a throwaway first

Exact-thread resume depends on the installed Hermes build: the service's Codex
adapter feature-detects `continuity=` / `model=` on
`CodexAppServerSession.__init__` (task `t_e39d314e`) and, where absent,
reports `exact_thread_resume: false` rather than claiming a capability the
build lacks.

Therefore, before the real thread is ever the subject of a migration:

1. Create a **throwaway** thread on the source host and put known content in it.
2. **Close its old process** — a migration attempted while the source process
   still holds the thread is not the case being tested.
3. Migrate it to the target host and confirm the exact thread id resumed and
   the known content is present.
4. Only a successful throwaway run authorises attempting the real thread.

**Never replace the real thread silently.** If exact resume is unavailable, the
exact reason must be reported and, if replacement is genuinely unavoidable, an
explicit continuity event must be recorded linking old thread → new thread with
the reason. A silent replacement would make the conversation look continuous
while its history was severed.

## 6. What is still UNKNOWN from this side

* Whether the installed Evo Hermes build exposes `continuity=` (decides
  §5 outcome). Read from the service's `describe()` at install time.
* The real service socket path/token location on Evo — the template ships a
  placeholder, deliberately.
* Whether the Workbench's own auth can reach the `serve` port; the bridge
  provides the URL, not the tunnel.
* Whether Hayden's Desktop build's remote-connection UI is on a version that
  accepts a bare `ws://host:port/api/ws`; the path is correct for this tree.
