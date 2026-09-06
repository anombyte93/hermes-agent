# Shared-conversation mode

One Astra conversation, four entrances, one model owner. Hayden types in the
Discord DM, opens the terminal, opens Hermes Desktop, opens Astra from the
Cortex Workbench — each shows the same conversation and continues it.

This document covers the **Hermes side** of that: an opt-in mode in
`tui_gateway` that turns a Hermes profile into a *client* of the Astra Evo
shared-conversation service instead of a model runner. The service itself,
its protocol, and its install live in the `astra-context-layer` repository
(`docs/evo-service.md`).

## What it is, in one paragraph

Ordinarily the gateway *is* the model runner: `session.create` builds an
`AIAgent`, `prompt.submit` runs a turn in-process. In shared mode the gateway
runs **no model at all**. `session.create` attaches to the service, replays its
transcript, and starts a poller; `prompt.submit` hands the message to the
owner and returns a receipt; the owner's reply arrives as ordinary
`message.start` / `message.delta` / `message.complete` frames. Nothing about
the TUI, the Desktop, or the browser Chat tab changes — they speak the same
JSON-RPC they always did.

## The gate

`shared_conversation.enabled: true` in the profile's `config.yaml`. That is
the entire switch. With the block absent or disabled, `maybe_dispatch` returns
`None` after one cached config read and every ordinary Hermes session behaves
exactly as before — no new code runs on the turn path.

Template: `docs/templates/astra-shared-conversation.config.yaml`.

## Install (client side only)

Nothing is installed automatically. Exact commands:

```bash
# 1. Create the client profile from the template.
docs/scripts/astra-shared.sh install
#    → ~/.hermes/profiles/astra-shared/config.yaml

# 2. Point it at the service and (for TCP) at the token file.
$EDITOR ~/.hermes/profiles/astra-shared/config.yaml

# 3. Read-only check: attaches, prints ping+status, detaches. Takes no
#    ownership, submits nothing, starts nothing.
docs/scripts/astra-shared.sh check
```

## Rollback

```bash
docs/scripts/astra-shared.sh rollback     # removes ~/.hermes/profiles/astra-shared
```

The client profile is the only thing removed. The service, its owner lease,
its conversation database and its immutable archive are never touched by
anything in this repository — a rollback here cannot lose a message.

## The four entrances, and the exact route for each

| # | Entrance | Route | Owned by |
|---|---|---|---|
| 1 | Discord DM | the service's own intake loop | astra-evo-service |
| 2 | Terminal | `hermes --profile astra-shared --tui` | this bridge |
| 3 | Hermes Desktop | Desktop's existing remote connection → `ws://<host>:<port>/api/ws` | this bridge |
| 4 | Browser / Cortex Workbench | `http://<host>:<port>/chat?profile=astra-shared` | this bridge |

Entrances 3 and 4 need the backend running under the shared profile:

```bash
docs/scripts/astra-shared.sh serve          # hermes --profile astra-shared serve
                                            # default 127.0.0.1:5199
```

### Why these routes and not a deep link

**There is no `hermes://` scheme handler on this machine, and this bridge does
not invent one.** Both remaining entrances reach Hermes over surfaces that
already exist, verified in this tree:

* **Entrance 3, `/api/ws`.** `tui_gateway/ws.py` documents itself as mounted at
  `/api/ws`, `hermes_cli/web_server.py` mounts it there, and
  `web/src/lib/gatewayClient.ts` connects to that exact path. Hermes Desktop's
  remote-connection config already takes a `wsUrl` of this shape (see
  `apps/desktop/src/app/gateway/hooks/use-gateway-boot`). Every frame on that
  socket goes through `server.dispatch`, which is where the shared-mode hook
  lives — so a Desktop client pointed at a shared-profile backend attaches
  with no Desktop change at all.
* **Entrance 4, `/chat?profile=`.** `/chat` is the dashboard's built-in Chat
  tab (`web/src/App.tsx` route `"/chat"`), and `?profile=` is an existing,
  honoured parameter: `ProfileProvider` propagates it through navigation, the
  `/api/pty` handler reads `ws.query_params.get("profile")`, and
  `_resolve_chat_argv` turns it into `env["HERMES_HOME"] = <profile dir>` for
  the spawned TUI **and its own gateway subprocess** (that function's docstring
  states the scoped case deliberately spawns its own gateway rather than
  attaching to the dashboard's). That child gateway therefore reads the shared
  profile's `config.yaml`, sees `shared_conversation.enabled`, and attaches.

So the Workbench link is a plain `http(s)` URL to a real, already-shipped
Hermes frontend — not an unhandled custom scheme, and not a new frontend. The
Workbench's own auth/tunnel decides how that URL is reached; no token belongs
in the URL, in `localStorage`, or in a repo.

## Guarantees, and their exact limits

| Guarantee | How | Limit |
|---|---|---|
| One owner | this process never builds an agent and never calls `run_turn` | the owner is the service; if it is down, messages queue |
| A client is not an owner | connect/disconnect/close only affect this attachment | — |
| Closing a client never stops the service | `detach` closes a socket and a poller, nothing else | — |
| Ordering | transcript `seq`, never source timestamps | a late arrival appears at its receipt position, by design |
| No lost message on a drop | poll-with-cursor; the cursor is client-held | — |
| Service restart recovery | a failed call closes the dead socket, the next poll reconnects from the same cursor | messages are delayed for the outage, never dropped |
| Honest receipts | `prompt.submit` returns the service's own accepted/duplicate/turn_id | **a receipt is not delivery and not execution** — the wording says so |
| Cross-client provenance | a message from another entrance is prefixed `[via <entrance>] <author>:` | the gateway protocol has no dedicated frame for it, so this prefix is the mechanism |

## Idempotency, retries, and what is deliberately NOT automatic

`client_message_id` is the service's idempotency key.

* A frontend that **supplies** it gets true retry semantics: resending the same
  key returns `duplicate: true` and queues no second turn.
* A frontend that **omits** it (today: the stock TUI/Desktop composer, which
  has no such field) gets a fresh UUID per RPC, so a resend is treated as a
  genuinely new message. That is correct for new text and is the only safe
  default for text we cannot prove is a retry.

**This bridge never auto-retries a submit.** If a submit's outcome is uncertain
(socket error after the frame went out), the error is surfaced and the resend
decision is the human's. An automatic resend under a fresh key would duplicate
a real message; under the same key it would silently swallow a genuine second
send. The poller reconnects; it does not replay submissions.

## Ordering: why events are held briefly

A frontend installs its session id from the `session.create` / `session.resume`
**response**. An event frame that overtook that response would name a session
the client has never heard of — and a transcript larger than one page makes
that race routine. So the bridge:

1. drains the whole backlog into the response;
2. starts the poller only after the response is built;
3. **holds** event frames until the attachment is *released* — by the next RPC
   naming that session (proof the client read the response) or by a 2s
   fallback timer, so a passive viewer is delayed, never starved.

## Refused methods, and the one that is now mapped

Methods that would start a second model runner or mutate the owner's
conversation from a client are refused with an explicit reason (code 5102) —
never silently passed to the local agent path, which is the exact bug this
mode exists to prevent: `prompt.background`, `session.branch`,
`session.compress`, `session.undo`, `session.steer`, `session.delete`,
`session.save`.

`session.interrupt` is **not** refused. The service exposes a real `stop`
method (`ControlAPI.stop_work`), so the terminal/Desktop interrupt is mapped
onto the owner's declared stop, idempotent on the client's key. The receipt
records the *declaration*, not the owner's completion of it.

## Known gaps (reported, not papered over)

* **Attachments/images are refused, not dropped.** Service protocol v1's
  `submit` takes `body`, `client_message_id`, `entrance`, `enqueue`,
  `work_key`, `sender_*` — there is no attachment or image parameter, and the
  service's own §7 records that its Codex adapter renders images as
  `[image attached]` so an image never reaches the model. Sending the text and
  discarding the image would make a client believe an attachment was
  delivered, so the whole submit is refused with
  `data.missing_seam = "submit.attachments"`. Image API work is owned
  elsewhere (task `t_2c7e41b6`); this bridge carries attachments the moment
  the service seam exists.
* **No server push.** Protocol v1 is poll-with-cursor (0.5–2s). Deliberate: a
  poll cannot lose a message when the socket drops; a push stream can.
* **No dedicated "another client typed this" frame.** Hence the `[via ...]`
  prefix above.

## Test scope — read this before quoting a result

* `tests/tui_gateway/test_shared_conversation_config.py` — the gate and the
  config refusals (inline secret, world-readable token file, unauthenticated
  non-loopback TCP, and the mode being inert when off).
* `tests/tui_gateway/test_shared_conversation_socket.py` — **early
  socket-only proof.** A real Unix-socket server speaking the documented
  protocol, driven through the real `server.dispatch`. Covers attach, replay,
  submit receipts, own-echo suppression, cross-entrance labelling, duplicate
  keys, disconnect, refusals, the attachment missing-seam, token auth, a real
  service stop/start recovery, and interrupt→stop. No model, no rendered UI.
* `tests/tui_gateway/test_shared_conversation_ws_entrance.py` — the same over
  the real `handle_ws` coroutine that `/api/ws` mounts, i.e. the Desktop and
  browser entrance's actual transport. Includes the ordering contract (no
  event frame precedes the attach response). Still no rendered UI, no model.

Rendered-client and real-model proof is explicitly **not** claimed by any test
in this repository; it follows the staged Evo install.
