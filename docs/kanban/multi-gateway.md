# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

Task subscriptions also cover review feedback. A `changes_requested` review
event is delivered as an actionable review-BLOCK notification. Subscriptions
using `notify+wake` additionally wake the exact originating chat/thread/session
so the controller inspects the existing card and current run; `notify` remains
passive-only and `wake` remains wake-only. Review feedback never creates,
unblocks, requeues, or otherwise mutates a task.

## Single-dispatcher posture

Only one gateway owns the kanban dispatcher. The owning gateway keeps
`kanban.dispatch_in_gateway: true` (the default); every other gateway sets it
to `false`.

**Why this matters:** dispatching is single-owner so multiple gateways do not
race to spawn the same work. Notification delivery is profile-owned instead:
each gateway polls only subscriptions for profiles whose platform adapters it
hosts. The atomic event claim prevents duplicate delivery across watcher
processes.

## Configuration

On the dispatch-owning gateway (typically the `default` profile), no change is
needed. On every other profile gateway, add to `~/.hermes/config.yaml`:

```yaml
kanban:
  dispatch_in_gateway: false
```

Or set the env var: `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`

## What each gateway does

| Gateway role | dispatch_in_gateway | Opens subscribed board DBs? | Dispatcher | Notifier |
|---|---|---|---|---|
| default (confirmed dispatch-lock owner) | true (default) | yes | yes | owned profiles + legacy unstamped subscriptions |
| writer, admin, coder, etc. | false | yes, when the profile has subscriptions | no | that gateway's owned profiles |

Non-dispatch gateways still deliver messages for their own platform adapters
(Telegram, Discord, etc.). They do not dispatch tasks, and they skip boards
that have no subscriptions owned by their profiles.

## Diagnosing "ready cards never start"

A card that is created and stays `ready` almost always means no gateway is
actually running the dispatcher for that board — even when a gateway process
is visibly alive. The reported case (issue #9) was a `hermes-gateway.service`
that had been failed and disabled for days sitting beside a still-running
*manual* gateway for the same profile: the process was serving Desktop
traffic, but nothing supervised it and its dispatcher state was invisible.

Two signals answer that directly:

```
hermes gateway status      # includes an "Ownership and dispatch" block
hermes gateway reconcile   # DRY RUN: prints the plan, changes nothing
```

**Ownership** is one of three answers, never a mix:

| Verdict | Meaning |
|---|---|
| `healthy` | the service is installed, active, its definition is current, and its `MainPID` IS the live listener |
| `DEGRADED` | something is running or installed but nobody cleanly owns it — a failed/disabled unit beside a live process, an outdated unit definition, an active unit whose `MainPID` is not the live listener, or an active unit with no listener at all |
| `stopped` | no service and no live listener |

**Dispatcher liveness** is a positive signal, not an inference from silence:
`{HERMES_HOME}/gateway_dispatcher.json` is stamped on *every* dispatcher tick,
idle or not, so "the dispatcher ticked and the board was idle" is
distinguishable from "no dispatcher is running". `last_dispatch_at` advances
only on a tick that actually spawned a worker. A record written by a process
that has since died reads as stale rather than continuing to vouch for it.

**Port conflicts** name their owner (PID, profile, process name) rather than
only reporting the port as occupied. The owner record deliberately carries no
argv/cmdline/environment field, so a conflict diagnostic cannot leak a
credential that happened to be on a command line.

## Reconciling ownership safely

`hermes gateway reconcile` is a dry run by default: it prints the verdict and
the smallest ordered plan that would move the box toward managed-healthy.

The load-bearing rule is that **there is never a second listener**:

- refreshing a stale unit definition starts nothing, so it is always in the
  plan when the definition is outdated;
- while a same-profile listener is alive, the plan contains **no start**;
- transferring ownership away from a live unmanaged process requires an
  explicit `--takeover`, which always stops the incumbent *before* starting
  the service;
- `--apply` re-probes for a live listener immediately before the start and
  refuses (`listener_already_live`) rather than racing it, so a plan built
  from a stale snapshot still cannot double-bind the port.

Re-running against an already-reconciled gateway is a no-op.

```
hermes gateway reconcile                 # diagnose, change nothing
hermes gateway reconcile --apply         # refresh unit / clear failed state
hermes gateway reconcile --apply --takeover   # stop the unmanaged process, then start the service
```

`--takeover` stops a gateway that may be serving live traffic. Run it when you
intend the handoff, not as a reflex.

