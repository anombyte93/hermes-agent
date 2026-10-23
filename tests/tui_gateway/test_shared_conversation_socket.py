"""Real-socket integration proof for shared-conversation mode.

**Scope label, read this first.** This is EARLY SOCKET-ONLY PROOF. It stands up
a real Unix-socket server speaking the documented service control protocol and
drives the real ``server.dispatch`` path, so the transport, framing, attach,
poll, own-echo suppression and receipt wording are exercised for real. It does
NOT prove the rendered TUI/Desktop/Workbench user path and it does NOT involve
a model: the fixture server below runs no model and the gateway in this mode
runs no model either. Full user-path proof follows the staged Evo install.

The fixture implements the contract from ``docs/evo-service.md`` §3 and mirrors
the real ``ControlAPI`` field-for-field (including ``source = "client:<entrance>"``
and ``source_native_id = client_message_id``), so a drift in either side shows
up as a failure here rather than at cutover.
"""

import json
import os
import socket
import socketserver
import threading
import time

import pytest

from tui_gateway import server as gw_server
from tui_gateway import shared_conversation as sc


# ─────────────────────────── fixture service ────────────────────────────


class FakeService:
    """Minimal, honest stand-in for the real ConversationEngine + ControlAPI."""

    def __init__(self, token=None):
        self.token = token
        self.lock = threading.Lock()
        self.messages = []
        self.seq = 0
        self.seen_native_ids = set()
        self.run_turn_calls = 0
        self.stop_calls = []
        self.conversation_id = "conv-test"

    # -- the only writer the gateway is allowed to reach --------------
    def submit(self, params):
        native_id = str(params.get("client_message_id") or "")
        if not native_id:
            return None, {"code": 400, "message": "client_message_id is required"}
        entrance = str(params.get("entrance") or "terminal")
        with self.lock:
            if native_id in self.seen_native_ids:
                return (
                    {
                        "message_id": f"dup-{native_id}",
                        "duplicate": True,
                        "accepted": False,
                        "turn_id": None,
                        "policy": "duplicate",
                        "chair_retained": False,
                        "retention_reasons": [],
                    },
                    None,
                )
            self.seen_native_ids.add(native_id)
            message_id = self._append(
                direction="inbound",
                source=f"client:{entrance}",
                source_native_id=native_id,
                sender_label=entrance,
                body=str(params.get("body") or ""),
            )
        return (
            {
                "message_id": message_id,
                "duplicate": False,
                "accepted": True,
                "turn_id": f"turn-{message_id}",
                "policy": "route",
                "chair_retained": False,
                "retention_reasons": [],
            },
            None,
        )

    # -- test helpers: the OWNER's side, never called by the gateway --
    def owner_replies(self, text):
        with self.lock:
            return self._append(
                direction="outbound",
                source="astra",
                source_native_id=f"out-{self.seq + 1}",
                sender_label="astra",
                body=text,
            )

    def other_entrance_says(self, text, *, entrance="discord", author="hayden"):
        with self.lock:
            return self._append(
                direction="inbound",
                source=entrance,
                source_native_id=f"ext-{self.seq + 1}",
                sender_label=author,
                body=text,
            )

    def _append(self, **row):
        self.seq += 1
        row.update(
            {
                "id": f"m{self.seq}",
                "conversation_id": self.conversation_id,
                "seq": self.seq,
                "work_id": None,
                "received_at": time.time(),
            }
        )
        self.messages.append(row)
        return row["id"]

    # -- protocol methods ---------------------------------------------
    def handle(self, request):
        rid = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") or {}
        if method != "ping" and self.token:
            presented = request.get("token")
            if presented != self.token:
                return {
                    "id": rid,
                    "ok": False,
                    "error": {"code": 401, "message": "unauthorised"},
                }
        if method == "ping":
            return {
                "id": rid,
                "ok": True,
                "result": {
                    "pong": True,
                    "protocol_version": 1,
                    "service": "astra-evo-service",
                    "version": "test",
                    "git_commit": "deadbeef",
                },
            }
        if method == "status":
            with self.lock:
                return {
                    "id": rid,
                    "ok": True,
                    "result": {
                        "conversation": {
                            "id": self.conversation_id,
                            "owner": "hayden",
                            "thread_ref": "thread-test",
                            "thread_state": "live",
                        },
                        "owner": {
                            "holder": "service",
                            "pid": 4242,
                            "held_by_this_process": True,
                            "heartbeat_stale": False,
                        },
                        "model": {"model": "gpt-5-codex", "provider": "codex"},
                        "stats": {"messages": len(self.messages)},
                    },
                }
        if method == "transcript":
            after = int(params.get("after_seq", 0))
            limit = min(int(params.get("limit", 100)), 500)
            with self.lock:
                rows = [m for m in self.messages if m["seq"] > after][:limit]
            return {
                "id": rid,
                "ok": True,
                "result": {
                    "conversation_id": self.conversation_id,
                    "messages": rows,
                    "next_after_seq": rows[-1]["seq"] if rows else after,
                },
            }
        if method == "submit":
            result, error = self.submit(params)
            if error:
                return {"id": rid, "ok": False, "error": error}
            return {"id": rid, "ok": True, "result": result}
        if method == "stop":
            # Mirrors ControlAPI.stop_work: archive-first accept, idempotent
            # on client_message_id, and it is a DECLARED stop.
            native_id = str(params.get("client_message_id") or "")
            if not native_id:
                return {
                    "id": rid,
                    "ok": False,
                    "error": {"code": 400, "message": "client_message_id is required"},
                }
            entrance = str(params.get("entrance") or "terminal")
            reason = str(params.get("reason") or "stop")
            with self.lock:
                self.stop_calls.append(
                    {
                        "client_message_id": native_id,
                        "entrance": entrance,
                        "reason": reason,
                    }
                )
                duplicate = native_id in self.seen_native_ids
                if duplicate:
                    return {
                        "id": rid,
                        "ok": True,
                        "result": {
                            "message_id": f"dup-{native_id}",
                            "duplicate": True,
                            "applied": False,
                            "reason": "this stop was already delivered and applied once",
                            "hold_state": "held",
                        },
                    }
                self.seen_native_ids.add(native_id)
                message_id = self._append(
                    direction="inbound",
                    source=f"client:{entrance}",
                    source_native_id=native_id,
                    sender_label=entrance,
                    body=reason,
                )
            return {
                "id": rid,
                "ok": True,
                "result": {
                    "message_id": message_id,
                    "duplicate": False,
                    "applied": True,
                    "hold_state": "held",
                },
            }
        if method == "run_turn":
            # Recorded so a test can assert the gateway NEVER calls it.
            with self.lock:
                self.run_turn_calls += 1
            return {"id": rid, "ok": True, "result": {"ran": False}}
        return {"id": rid, "ok": False, "error": {"code": 404, "message": method}}


class _Handler(socketserver.StreamRequestHandler):
    service: FakeService

    def handle(self):
        while True:
            line = self.rfile.readline()
            if not line:
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except ValueError:
                response = {"ok": False, "error": {"code": 400, "message": "bad frame"}}
            else:
                response = self.service.handle(request)
            self.wfile.write(json.dumps(response, default=str).encode("utf-8") + b"\n")
            self.wfile.flush()


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def service(tmp_path):
    svc = FakeService()
    path = str(tmp_path / "svc.sock")

    handler = type("Handler", (_Handler,), {"service": svc})
    srv = _UnixServer(path, handler)
    os.chmod(path, 0o600)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    svc.address = path
    try:
        yield svc
    finally:
        srv.shutdown()
        srv.server_close()


class RecordingTransport:
    """Stands in for a WS/stdio client peer; records every frame written."""

    def __init__(self):
        self.frames = []
        self.open = True
        self.lock = threading.Lock()

    def write(self, obj):
        with self.lock:
            if not self.open:
                return False
            self.frames.append(obj)
        return True

    def close(self):
        with self.lock:
            self.open = False

    def events(self, etype=None):
        with self.lock:
            frames = list(self.frames)
        out = [f for f in frames if f.get("method") == "event"]
        if etype:
            out = [f for f in out if f["params"].get("type") == etype]
        return out


@pytest.fixture
def shared_cfg(service, monkeypatch):
    cfg = sc.SharedConversationConfig(address=service.address, poll_interval=0.05)
    monkeypatch.setattr(sc, "active_config", lambda force=False: cfg)
    monkeypatch.setattr(sc, "config_error", lambda: None)
    yield cfg
    sc.detach_all(reason="test-teardown")


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _rpc(method, params, transport):
    resp = gw_server.dispatch(
        {"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        transport,
    )
    # Frames are held until the client proves it holds its session id (a later
    # RPC, or the fallback timer). Tests that assert on live frames drive that
    # release explicitly here so they never depend on the timer's timing.
    if method in {"session.create", "session.resume", "session.most_recent"}:
        sid = (resp or {}).get("result", {}).get("session_id")
        if sid:
            sc.release(sid)
    return resp


# ───────────────────────────── the proof ────────────────────────────────


def test_session_create_attaches_and_replays_the_shared_transcript(
    service, shared_cfg
):
    service.other_entrance_says("hello from the DM", entrance="discord")
    service.owner_replies("hello back")
    transport = RecordingTransport()

    resp = _rpc("session.create", {"cols": 80}, transport)

    result = resp["result"]
    assert result["stored_session_id"] == "conv-test"
    texts = [m["text"] for m in result["messages"]]
    assert "[via discord] hayden: hello from the DM" in texts
    assert "hello back" in texts
    # The info block tells the client the truth about ownership.
    shared = result["info"]["shared_conversation"]
    assert shared["attached"] is True
    assert shared["held_by_this_process"] is False
    assert shared["conversation_id"] == "conv-test"


def test_prompt_submit_queues_with_the_owner_and_never_runs_a_turn(
    service, shared_cfg
):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]

    resp = _rpc(
        "prompt.submit",
        {"session_id": sid, "text": "nonce-terminal-A1", "client_message_id": "k1"},
        transport,
    )

    receipt = resp["result"]["shared_conversation"]
    assert receipt["accepted"] is True
    assert receipt["duplicate"] is False
    # Receipt wording must not claim execution or delivery.
    assert "not proof of execution or delivery" in receipt["note"]
    assert "delivered" not in receipt["note"]
    # The message really reached the service.
    assert any(m["body"] == "nonce-terminal-A1" for m in service.messages)
    # And the gateway never dispatched a turn — that is the owner's job.
    assert service.run_turn_calls == 0


def test_owner_reply_streams_to_the_client_as_message_frames(service, shared_cfg):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]
    _rpc(
        "prompt.submit",
        {"session_id": sid, "text": "ping", "client_message_id": "k2"},
        transport,
    )

    service.owner_replies("nonce-owner-reply-B2")

    assert _wait_for(
        lambda: any(
            f["params"]["payload"].get("text") == "nonce-owner-reply-B2"
            for f in transport.events("message.complete")
        )
    ), [f["params"] for f in transport.events()]
    # The full frame triple the TUI/Desktop renderers switch on.
    assert transport.events("message.start")
    assert any(
        f["params"]["payload"].get("text") == "nonce-owner-reply-B2"
        for f in transport.events("message.delta")
    )


def test_a_clients_own_message_is_not_echoed_back_at_it(service, shared_cfg):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]

    _rpc(
        "prompt.submit",
        {"session_id": sid, "text": "nonce-own-C3", "client_message_id": "k3"},
        transport,
    )
    service.owner_replies("acknowledged")
    assert _wait_for(
        lambda: any(
            f["params"]["payload"].get("text") == "acknowledged"
            for f in transport.events("message.complete")
        )
    )

    completes = [
        f["params"]["payload"].get("text") for f in transport.events("message.complete")
    ]
    assert "nonce-own-C3" not in completes


def test_another_entrance_is_labelled_never_disguised_as_this_client(
    service, shared_cfg
):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]
    assert sid

    service.other_entrance_says("nonce-discord-D4", entrance="discord", author="hayden")

    assert _wait_for(
        lambda: any(
            "nonce-discord-D4" in (f["params"]["payload"].get("text") or "")
            for f in transport.events("message.complete")
        )
    )
    frame = next(
        f
        for f in transport.events("message.complete")
        if "nonce-discord-D4" in (f["params"]["payload"].get("text") or "")
    )
    assert frame["params"]["payload"]["text"].startswith("[via discord] hayden:")
    assert frame["params"]["payload"]["display_metadata"]["shared_role"] == "user"


def test_two_entrances_see_the_same_conversation(service, shared_cfg):
    terminal = RecordingTransport()
    desktop = RecordingTransport()
    t_sid = _rpc("session.create", {"entrance": "terminal"}, terminal)["result"][
        "session_id"
    ]
    d_sid = _rpc("session.create", {"entrance": "desktop"}, desktop)["result"][
        "session_id"
    ]
    assert t_sid != d_sid  # two clients, two attachments

    _rpc(
        "prompt.submit",
        {"session_id": t_sid, "text": "nonce-terminal-E5", "client_message_id": "k5"},
        terminal,
    )

    # The desktop client sees the terminal's message, labelled with its entrance.
    assert _wait_for(
        lambda: any(
            "nonce-terminal-E5" in (f["params"]["payload"].get("text") or "")
            for f in desktop.events("message.complete")
        )
    )
    frame = next(
        f
        for f in desktop.events("message.complete")
        if "nonce-terminal-E5" in (f["params"]["payload"].get("text") or "")
    )
    assert frame["params"]["payload"]["text"].startswith("[via terminal]")
    # And a resume on a third client replays both entrances' history.
    third = RecordingTransport()
    resumed = _rpc("session.resume", {}, third)["result"]
    assert any("nonce-terminal-E5" in m["text"] for m in resumed["messages"])


def test_closing_a_client_detaches_it_and_never_stops_the_service(
    service, shared_cfg
):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]
    assert sid in sc.attachments()

    resp = _rpc("session.close", {"session_id": sid}, transport)

    assert resp["result"]["shared_conversation"]["service_stopped"] is False
    assert sid not in sc.attachments()
    # The service is still fully usable by a fresh client.
    again = RecordingTransport()
    assert _rpc("session.create", {}, again)["result"]["stored_session_id"] == (
        "conv-test"
    )


def test_disconnect_detaches_without_touching_the_conversation(service, shared_cfg):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]
    service.owner_replies("first")
    assert _wait_for(lambda: transport.events("message.complete"))

    transport.close()  # peer gone, as on a WS drop
    service.owner_replies("second")

    assert _wait_for(lambda: sid not in sc.attachments())
    # The conversation kept both messages; nothing was lost or rolled back.
    assert [m["body"] for m in service.messages if m["direction"] == "outbound"] == [
        "first",
        "second",
    ]


def test_duplicate_submit_is_reported_as_duplicate_not_as_a_second_send(
    service, shared_cfg
):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]
    params = {"session_id": sid, "text": "retry me", "client_message_id": "k-dup"}

    first = _rpc("prompt.submit", params, transport)["result"]
    second = _rpc("prompt.submit", params, transport)["result"]

    assert first["shared_conversation"]["duplicate"] is False
    assert second["shared_conversation"]["duplicate"] is True
    assert first["status"] == "streaming"
    assert second["status"] == "idle"  # no second busy latch
    assert sum(1 for m in service.messages if m["body"] == "retry me") == 1


def test_owner_mutating_methods_are_refused_not_run_locally(service, shared_cfg):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]

    for method in ("prompt.background", "session.branch", "session.compress",
                   "session.undo", "session.delete", "session.steer"):
        resp = _rpc(method, {"session_id": sid}, transport)
        assert resp["error"]["code"] == sc.ERR_REFUSED_METHOD, method


def test_attachments_are_refused_with_the_exact_missing_seam(service, shared_cfg):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]

    resp = _rpc(
        "prompt.submit",
        {
            "session_id": sid,
            "text": "look at this",
            "client_message_id": "k-img",
            "images": [{"path": "/tmp/x.png"}],
        },
        transport,
    )

    assert resp["error"]["code"] == sc.ERR_REFUSED_METHOD
    assert resp["error"]["data"]["missing_seam"] == "submit.attachments"
    # Nothing was sent: a partial text-only send would misrepresent delivery.
    assert not any(m["body"] == "look at this" for m in service.messages)


def test_submit_without_an_attachment_needs_an_attachment_first(service, shared_cfg):
    transport = RecordingTransport()
    resp = _rpc(
        "prompt.submit",
        {"session_id": "nope", "text": "hi", "client_message_id": "k-x"},
        transport,
    )
    assert resp["error"]["code"] == sc.ERR_UNAVAILABLE


def test_unreachable_service_warns_and_never_falls_back_to_a_local_agent(
    tmp_path, monkeypatch
):
    cfg = sc.SharedConversationConfig(
        address=str(tmp_path / "absent.sock"), connect_timeout=0.5
    )
    monkeypatch.setattr(sc, "active_config", lambda force=False: cfg)
    monkeypatch.setattr(sc, "config_error", lambda: None)
    transport = RecordingTransport()

    resp = _rpc("session.create", {}, transport)

    assert resp["error"]["code"] == sc.ERR_UNAVAILABLE
    assert "unavailable" in resp["error"]["message"]


def test_token_auth_is_presented_on_every_call(tmp_path, monkeypatch):
    svc = FakeService(token="right-token")
    path = str(tmp_path / "auth.sock")
    handler = type("Handler", (_Handler,), {"service": svc})
    srv = _UnixServer(path, handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        good = sc.SharedConversationConfig(address=path, token="right-token",
                                           poll_interval=0.05)
        monkeypatch.setattr(sc, "active_config", lambda force=False: good)
        monkeypatch.setattr(sc, "config_error", lambda: None)
        transport = RecordingTransport()
        assert "result" in _rpc("session.create", {}, transport)
        sc.detach_all(reason="test")

        bad = sc.SharedConversationConfig(address=path, token="wrong",
                                          poll_interval=0.05)
        monkeypatch.setattr(sc, "active_config", lambda force=False: bad)
        resp = _rpc("session.create", {}, RecordingTransport())
        assert resp["error"]["code"] == sc.ERR_UNAVAILABLE
        assert "401" in resp["error"]["message"]
    finally:
        sc.detach_all(reason="test")
        srv.shutdown()
        srv.server_close()


def test_socket_permissions_are_owner_only(service):
    assert oct(os.stat(service.address).st_mode)[-3:] == "600"
    assert socket.AF_UNIX  # the transport really is a Unix socket


# ─────────────── parent review findings: recovery + stop ────────────────


def test_client_recovers_when_the_service_restarts_under_it(tmp_path, monkeypatch):
    """A live service restart must not permanently wedge an attached client.

    Real stop/start of the socket server with the SAME client attached: the
    dead stream is dropped on error and the next poll reconnects, resuming
    from the client-held cursor (no replay of what was already rendered, no
    loss of what arrived while the service was down).
    """
    svc = FakeService()
    path = str(tmp_path / "restart.sock")

    def serve():
        # Unix sockets are not auto-unlinked by server_close; a restart on the
        # same path must clear the stale node first (this is what the real
        # installer does too).
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        handler = type("Handler", (_Handler,), {"service": svc})
        srv = _UnixServer(path, handler)
        os.chmod(path, 0o600)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        return srv

    srv = serve()
    cfg = sc.SharedConversationConfig(address=path, poll_interval=0.05,
                                      connect_timeout=2.0)
    monkeypatch.setattr(sc, "active_config", lambda force=False: cfg)
    monkeypatch.setattr(sc, "config_error", lambda: None)
    transport = RecordingTransport()
    try:
        sid = _rpc("session.create", {}, transport)["result"]["session_id"]
        sc.release(sid)
        svc.owner_replies("before-restart")
        assert _wait_for(
            lambda: any(
                f["params"]["payload"].get("text") == "before-restart"
                for f in transport.events("message.complete")
            )
        )
        cursor_before = sc.attachments()[sid].cursor

        # Real restart: same socket path, same in-memory conversation.
        srv.shutdown()
        srv.server_close()
        time.sleep(0.2)
        svc.owner_replies("during-outage")
        srv = serve()

        assert _wait_for(
            lambda: any(
                f["params"]["payload"].get("text") == "during-outage"
                for f in transport.events("message.complete")
            ),
            timeout=15.0,
        ), "client never recovered after the service restarted"
        # The pre-restart message was NOT re-rendered: the cursor held.
        assert (
            sum(
                1
                for f in transport.events("message.complete")
                if f["params"]["payload"].get("text") == "before-restart"
            )
            == 1
        )
        assert sc.attachments()[sid].cursor > cursor_before
        # And the client is still attached, having submitted nothing on its own.
        assert svc.run_turn_calls == 0
    finally:
        sc.detach_all(reason="test")
        srv.shutdown()
        srv.server_close()


def test_interrupt_maps_to_the_service_stop_method(service, shared_cfg):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]

    resp = _rpc(
        "session.interrupt",
        {"session_id": sid, "client_message_id": "stop-1", "reason": "stop now"},
        transport,
    )

    assert resp["result"]["interrupted"] is True
    assert service.stop_calls == [
        {"client_message_id": "stop-1", "entrance": "terminal", "reason": "stop now"}
    ]
    # Receipt records the declaration, not its completion.
    note = resp["result"]["shared_conversation"]["note"]
    assert "declaration" in note


def test_repeat_interrupt_with_the_same_key_is_reported_as_duplicate(
    service, shared_cfg
):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]
    params = {"session_id": sid, "client_message_id": "stop-dup"}

    first = _rpc("session.interrupt", params, transport)["result"]
    second = _rpc("session.interrupt", params, transport)["result"]

    assert first["interrupted"] is True
    assert second["interrupted"] is False
    assert second["shared_conversation"]["duplicate"] is True


def test_a_stable_client_key_makes_a_retry_idempotent(service, shared_cfg):
    """A frontend that supplies a stable key gets true retry semantics."""
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]

    a = _rpc(
        "prompt.submit",
        {"session_id": sid, "text": "same text", "client_message_id": "stable"},
        transport,
    )["result"]
    b = _rpc(
        "prompt.submit",
        {"session_id": sid, "text": "same text", "client_message_id": "stable"},
        transport,
    )["result"]
    # Genuinely new text under a NEW key is accepted, not swallowed.
    c = _rpc(
        "prompt.submit",
        {"session_id": sid, "text": "same text", "client_message_id": "fresh"},
        transport,
    )["result"]

    assert a["shared_conversation"]["duplicate"] is False
    assert b["shared_conversation"]["duplicate"] is True
    assert c["shared_conversation"]["duplicate"] is False
    assert sum(1 for m in service.messages if m["body"] == "same text") == 2


def test_an_uncertain_submit_is_surfaced_and_never_auto_retried(
    service, shared_cfg, monkeypatch
):
    transport = RecordingTransport()
    sid = _rpc("session.create", {}, transport)["result"]["session_id"]
    attachment = sc.attachments()[sid]

    calls = []
    real_call = attachment.client.call

    def flaky(method, **params):
        calls.append(method)
        if method == "submit":
            raise sc.AttachError("connection closed by the service")
        return real_call(method, **params)

    monkeypatch.setattr(attachment.client, "call", flaky)

    resp = _rpc(
        "prompt.submit",
        {"session_id": sid, "text": "uncertain", "client_message_id": "u1"},
        transport,
    )

    assert resp["error"]["code"] == sc.ERR_UNAVAILABLE
    # Exactly ONE submit attempt: no silent replay of an uncertain send.
    assert calls.count("submit") == 1

