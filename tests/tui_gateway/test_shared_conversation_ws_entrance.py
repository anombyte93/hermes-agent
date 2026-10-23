"""The Desktop / browser-Workbench entrance: shared mode over the real WS path.

This drives ``tui_gateway.ws.handle_ws`` — the exact coroutine ``hermes serve``
and ``hermes dashboard`` mount at ``/api/ws`` (see the module docstring of
``tui_gateway/ws.py`` and ``web/src/lib/gatewayClient.ts``, which connects to
that path). Hermes Desktop and the dashboard's Chat tab are both clients of it,
so proving shared mode over ``handle_ws`` proves the entrance those surfaces
actually use.

**Scope label:** early real-transport proof. Real frames over the real WS
dispatch path with a real socket-backed service fixture, but no rendered
Desktop UI and no model. Rendered-client proof follows the staged Evo install.
"""

import asyncio
import json

import pytest

from tui_gateway import shared_conversation as sc
from tui_gateway import ws as ws_mod

from tests.tui_gateway.test_shared_conversation_socket import (  # noqa: F401
    FakeService,
    service,
    shared_cfg,
)


class ScriptedWS:
    """A fastapi-shaped WebSocket driven by an explicit frame queue.

    Frames are pushed with :meth:`send` and the socket stays open until
    :meth:`hangup` — so a test can wait for an attach, then submit over the
    SAME socket, exactly as a real client does.
    """

    _HANGUP = object()

    def __init__(self, requests=()):
        self.sent = []
        self.accepted = False
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()
        for request in requests:
            self._queue.put_nowait(request)

    def send(self, request):
        self._queue.put_nowait(request)

    def hangup(self):
        self._queue.put_nowait(self._HANGUP)

    async def accept(self):
        self.accepted = True

    async def send_text(self, line):
        self.sent.append(json.loads(line))

    async def receive_text(self):
        item = await self._queue.get()
        if item is self._HANGUP:
            raise ws_mod._WebSocketDisconnect()
        return json.dumps(item)

    async def close(self, *args, **kwargs):
        self.closed = True

    # Some code paths probe these; keep them inert.
    @property
    def client(self):
        return None

    @property
    def headers(self):
        return {}

    @property
    def query_params(self):
        return {}


def _frames(ws, kind=None):
    events = [f for f in ws.sent if f.get("method") == "event"]
    if kind:
        events = [f for f in events if f["params"].get("type") == kind]
    return events


def _results(ws):
    return [f for f in ws.sent if "result" in f]


def test_desktop_ws_entrance_attaches_submits_and_receives_the_owner_reply(
    service, shared_cfg  # noqa: F811
):
    service.other_entrance_says("earlier DM line", entrance="discord")

    ws = ScriptedWS()

    async def drive():
        task = asyncio.create_task(ws_mod.handle_ws(ws))
        ws.send(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "session.create",
                "params": {"entrance": "desktop"},
            }
        )
        for _ in range(300):
            if sc.attachments():
                break
            await asyncio.sleep(0.02)
        sid = next(iter(sc.attachments()))
        ws.send(
            {
                "jsonrpc": "2.0",
                "id": "2",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "nonce-desktop-W1",
                    "client_message_id": "ws-k1",
                },
            }
        )
        for _ in range(300):
            if any(m["body"] == "nonce-desktop-W1" for m in service.messages):
                break
            await asyncio.sleep(0.02)
        service.owner_replies("nonce-owner-W2")
        for _ in range(400):
            if any(
                f["params"]["payload"].get("text") == "nonce-owner-W2"
                for f in _frames(ws, "message.complete")
            ):
                break
            await asyncio.sleep(0.02)
        ws.hangup()
        await task

    asyncio.run(drive())

    # The create response carried the shared transcript and honest ownership.
    create = next(f for f in _results(ws) if f.get("id") == "1")
    assert create["result"]["stored_session_id"] == "conv-test"
    assert any(
        "earlier DM line" in m["text"] for m in create["result"]["messages"]
    )
    assert (
        create["result"]["info"]["shared_conversation"]["held_by_this_process"]
        is False
    )
    # The submit receipt never claims execution or delivery.
    submit = next(f for f in _results(ws) if f.get("id") == "2")
    assert submit["result"]["shared_conversation"]["accepted"] is True
    assert "not proof of execution or delivery" in (
        submit["result"]["shared_conversation"]["note"]
    )
    # The owner's reply arrived over the websocket as real message frames.
    assert any(
        f["params"]["payload"].get("text") == "nonce-owner-W2"
        for f in _frames(ws, "message.complete")
    )
    assert service.run_turn_calls == 0


def test_no_event_frame_precedes_the_attach_response(service, shared_cfg):  # noqa: F811
    """Ordering contract: the client learns its session id before any event.

    A frontend installs the session id from the ``session.create`` RESPONSE.
    Any event frame written before it would name a session the client has
    never heard of, so the bridge holds frames until release.
    """
    for i in range(12):
        service.other_entrance_says(f"backlog {i}", entrance="discord")

    ws = ScriptedWS()

    async def drive():
        task = asyncio.create_task(ws_mod.handle_ws(ws))
        ws.send(
            {"jsonrpc": "2.0", "id": "1", "method": "session.create", "params": {}}
        )
        for _ in range(300):
            if any(f.get("id") == "1" for f in _results(ws)):
                break
            await asyncio.sleep(0.02)
        service.owner_replies("post-attach line")
        for _ in range(400):
            if _frames(ws, "message.complete"):
                break
            await asyncio.sleep(0.02)
        ws.hangup()
        await task

    asyncio.run(drive())

    index_of_response = next(
        i for i, f in enumerate(ws.sent) if f.get("id") == "1" and "result" in f
    )
    session_events = [
        i
        for i, f in enumerate(ws.sent)
        if f.get("method") == "event"
        and f["params"].get("session_id")
        and f["params"]["session_id"] != ""
        and f["params"].get("type") != "gateway.ready"
    ]
    assert all(i > index_of_response for i in session_events), ws.sent
    # And the backlog was delivered in the response, not replayed as live.
    create = ws.sent[index_of_response]
    assert len(create["result"]["messages"]) >= 12
    live_texts = [
        f["params"]["payload"].get("text") for f in _frames(ws, "message.complete")
    ]
    assert not any("backlog" in (t or "") for t in live_texts)


def test_ws_disconnect_detaches_the_client_and_leaves_the_service_running(
    service, shared_cfg  # noqa: F811
):
    ws = ScriptedWS(
        [{"jsonrpc": "2.0", "id": "1", "method": "session.create", "params": {}}]
    )
    ws.hangup()
    asyncio.run(ws_mod.handle_ws(ws))

    # Socket closed → attachment gone.
    assert sc.attachments() == {}
    # The conversation is intact and a fresh client sees it.
    service.owner_replies("still alive")
    ws2 = ScriptedWS(
        [{"jsonrpc": "2.0", "id": "1", "method": "session.resume", "params": {}}]
    )
    ws2.hangup()
    asyncio.run(ws_mod.handle_ws(ws2))
    resume = next(f for f in _results(ws2) if f.get("id") == "1")
    assert any("still alive" in m["text"] for m in resume["result"]["messages"])


def test_two_ws_clients_share_one_conversation(service, shared_cfg):  # noqa: F811
    ws_a = ScriptedWS()

    async def drive():
        task_a = asyncio.create_task(ws_mod.handle_ws(ws_a))
        ws_a.send(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "session.create",
                "params": {"entrance": "workbench"},
            }
        )
        for _ in range(300):
            if sc.attachments():
                break
            await asyncio.sleep(0.02)
        sid_a = next(iter(sc.attachments()))
        ws_a.send(
            {
                "jsonrpc": "2.0",
                "id": "2",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid_a,
                    "text": "nonce-workbench-W3",
                    "client_message_id": "ws-k3",
                },
            }
        )
        for _ in range(300):
            if any(m["body"] == "nonce-workbench-W3" for m in service.messages):
                break
            await asyncio.sleep(0.02)
        ws_a.hangup()
        await task_a

    asyncio.run(drive())

    # A second entrance opened afterwards replays the workbench message with
    # its provenance intact — same conversation, labelled honestly.
    ws_b = ScriptedWS(
        [
            {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "session.resume",
                "params": {"entrance": "desktop"},
            }
        ]
    )
    ws_b.hangup()
    asyncio.run(ws_mod.handle_ws(ws_b))
    resume = next(f for f in _results(ws_b) if f.get("id") == "1")
    texts = [m["text"] for m in resume["result"]["messages"]]
    assert "[via workbench] workbench: nonce-workbench-W3" in texts
