"""Opt-in shared-conversation mode: attach the TUI/Desktop to ONE remote owner.

Ordinary Hermes sessions never touch this module. It activates only when the
active profile's ``config.yaml`` contains a ``shared_conversation`` block with
``enabled: true`` — that is the whole gate. With no such block,
:func:`maybe_dispatch` returns ``None`` after one cached config read and the
gateway behaves exactly as before.

What the mode does
------------------
The gateway normally *is* the model runner: ``session.create`` builds an
``AIAgent`` and ``prompt.submit`` runs a turn in this process. In shared mode
this process runs **no model at all**. It becomes a client of the Astra Evo
shared-conversation service (newline-delimited JSON-RPC over a Unix or TCP
socket, documented in ``docs/evo-service.md`` of the astra-context-layer
repository), which owns the single serial conversation that the Discord DM,
the terminal, Hermes Desktop and the Cortex Workbench all attach to.

Consequences that are deliberate, not incidental:

* **A client is not an owner.** Connect/disconnect/reconnect and closing the
  terminal have zero effect on the service, the owner lease or the thread. We
  never call ``run_turn`` — dispatching a queued turn is the owner's job.
* **Receipts never claim execution or delivery.** ``prompt.submit`` returns
  what the service actually said (accepted / duplicate / turn id) under a
  ``shared_conversation`` key, and the human-facing wording is "queued with
  the service owner", never "sent" or "answered".
* **Methods that would start a second model runner are refused**, not
  silently forwarded to the local agent path (see :data:`_REFUSED`).

Transport shape
---------------
The frontends are unchanged. We speak the exact frames ``apps/desktop`` and
``ui-tui`` already implement:

* ``session.create`` / ``session.resume`` / ``session.most_recent`` answer with
  ``{session_id, info, messages[]}`` — ``messages`` is the real service
  transcript projected onto the gateway's ``{role, text}`` shape.
* Live traffic arrives as ``message.start`` → ``message.delta`` →
  ``message.complete`` event frames, plus ``status.update`` and
  ``notification.show`` for service health. Those are the frames
  ``ui-tui/src/app/createGatewayEventHandler.ts`` and the Desktop renderer
  already switch on; nothing new was invented for this mode.

Known limitation (not papered over): protocol v1 of the service has no server
push, so a poller holds a ``transcript`` cursor per attached client (§3 of
``docs/evo-service.md``), and the TUI/Desktop protocol has no frame for "a
message from *another* human client arrived". Such messages are rendered as
assistant-channel frames carrying an explicit ``[via <entrance>] <author>:``
prefix rather than being disguised as this client's own input.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

CONFIG_KEY = "shared_conversation"

# Config re-read interval. The underlying ``server._load_cfg`` is mtime-cached,
# but it still stats a file; a few seconds of staleness on a mode that is
# switched by editing config.yaml is a fair trade for keeping ordinary RPC
# dispatch free of per-call filesystem work.
_CONFIG_TTL_S = 5.0

_DEFAULT_POLL_INTERVAL_S = 0.75
_DEFAULT_CONNECT_TIMEOUT_S = 10.0
_DEFAULT_TRANSCRIPT_LIMIT = 200

# JSON-RPC error codes used by this module. 5100+ is unused by the gateway
# today; keeping our own band makes a shared-mode refusal unmistakable in a
# client log.
ERR_UNAVAILABLE = 5101       # the service could not be reached
ERR_REFUSED_METHOD = 5102    # method would start a second model runner
ERR_NOT_ACCEPTED = 5103      # the service declined the submission


# ────────────────────────────── client ──────────────────────────────
#
# Vendored from ``astra_context_layer/evo_service/attach_client.py`` (that
# module documents itself as dependency-free and vendorable). Kept here so the
# gateway has no import dependency on the service package, which is deployed
# separately and must not be modified by this card.


class AttachError(RuntimeError):
    """The service refused a call, or the socket went away mid-call."""


class AttachClient:
    """Minimal newline-JSON client for the service control socket."""

    def __init__(
        self,
        address: str | tuple[str, int],
        *,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.address = address
        self._token = token
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._file: Any = None
        self._lock = threading.Lock()

    def connect(self) -> "AttachClient":
        if isinstance(self.address, tuple):
            sock = socket.create_connection(self.address, timeout=self.timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.address)
        self._sock = sock
        self._file = sock.makefile("rwb")
        return self

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

    def call(self, method: str, **params: Any) -> Any:
        """One request/response round trip.

        Serialised: the service protocol is one frame per line on a single
        connection, so two threads (an RPC handler and the poller) sharing a
        client would interleave their frames. Each attachment therefore owns
        its own client, and this lock covers the remaining overlap.

        On ANY transport failure the socket is closed before the error is
        raised. A half-dead file object left in place would make every later
        call fail forever against a stream the service has already hung up —
        which is exactly what happens on a service restart. Closing here makes
        the next call reconnect (see :meth:`_connect_locked`).
        """
        with self._lock:
            if self._file is None:
                try:
                    self._connect_locked()
                except OSError as exc:
                    raise AttachError(f"cannot reach the service: {exc}") from exc
            assert self._file is not None
            request: dict[str, Any] = {
                "id": uuid.uuid4().hex,
                "method": method,
                "params": params,
            }
            if self._token:
                request["token"] = self._token
            try:
                self._file.write(json.dumps(request).encode("utf-8") + b"\n")
                self._file.flush()
                line = self._file.readline()
            except OSError as exc:
                self._drop_locked()
                raise AttachError(f"service socket error: {exc}") from exc
            if not line:
                self._drop_locked()
                raise AttachError("connection closed by the service")
            try:
                response = json.loads(line.decode("utf-8"))
            except ValueError as exc:
                self._drop_locked()
                raise AttachError(f"malformed service frame: {exc}") from exc
        if not response.get("ok"):
            error = response.get("error") or {}
            raise AttachError(f"{error.get('code')}: {error.get('message')}")
        return response.get("result")

    def _drop_locked(self) -> None:
        """Discard a dead connection so the next call reconnects. Lock held."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _connect_locked(self) -> None:
        if isinstance(self.address, tuple):
            sock = socket.create_connection(self.address, timeout=self.timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.address)
        self._sock = sock
        self._file = sock.makefile("rwb")

    # convenience wrappers -------------------------------------------------

    def ping(self) -> dict:
        return self.call("ping") or {}

    def status(self) -> dict:
        return self.call("status") or {}

    def transcript(self, after_seq: int = 0, limit: int = 100) -> dict:
        return self.call("transcript", after_seq=after_seq, limit=limit) or {}

    def submit(
        self,
        body: str,
        *,
        entrance: str,
        client_message_id: str,
        enqueue: bool = True,
    ) -> dict:
        return self.call(
            "submit",
            body=body,
            entrance=entrance,
            client_message_id=client_message_id,
            enqueue=enqueue,
        ) or {}


# ────────────────────────────── config ──────────────────────────────


@dataclass(frozen=True)
class SharedConversationConfig:
    address: str | tuple[str, int]
    token: str | None = None
    poll_interval: float = _DEFAULT_POLL_INTERVAL_S
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_S
    transcript_limit: int = _DEFAULT_TRANSCRIPT_LIMIT
    entrance: str | None = None
    label: str = "Astra (shared)"


class ConfigError(ValueError):
    """The ``shared_conversation`` block exists but is not usable."""


def _resolve_secret(ref: Any, *, field_name: str) -> str | None:
    """Resolve an ``env:NAME`` / ``file:/path`` secret reference.

    Mirrors the service's own rule (``docs/evo-service.md`` §4): an inline
    literal is rejected so a token cannot be committed to config.yaml, and a
    ``file:`` secret must not be group/world readable.
    """
    if ref is None:
        return None
    if not isinstance(ref, str) or not ref.strip():
        raise ConfigError(f"{field_name}: must be a non-empty string reference")
    ref = ref.strip()
    if ref.startswith("env:"):
        name = ref[4:].strip()
        if not name:
            raise ConfigError(f"{field_name}: 'env:' reference names no variable")
        value = os.environ.get(name)
        if not value:
            raise ConfigError(f"{field_name}: environment variable {name} is unset")
        return value
    if ref.startswith("file:"):
        path = os.path.expanduser(ref[5:].strip())
        if not path:
            raise ConfigError(f"{field_name}: 'file:' reference names no path")
        try:
            mode = os.stat(path).st_mode
        except OSError as exc:
            raise ConfigError(f"{field_name}: cannot read {path}: {exc}") from exc
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            raise ConfigError(
                f"{field_name}: {path} is group/world readable; chmod 600 it"
            )
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if not value:
            raise ConfigError(f"{field_name}: {path} is empty")
        return value
    raise ConfigError(
        f"{field_name}: must be 'env:NAME' or 'file:/path' — an inline secret "
        "in config.yaml is refused"
    )


def parse_config(block: Any) -> SharedConversationConfig | None:
    """Parse the ``shared_conversation`` config block.

    Returns ``None`` when the mode is absent or explicitly disabled. Raises
    :class:`ConfigError` when the block is present, enabled, and wrong — a
    misconfigured shared mode must be loud, never a silent fallback to a
    local model runner.
    """
    if not isinstance(block, dict):
        return None
    if not bool(block.get("enabled")):
        return None

    socket_path = (block.get("socket_path") or "").strip() if block.get("socket_path") else ""
    tcp_host = (block.get("tcp_host") or "").strip() if block.get("tcp_host") else ""
    tcp_port = block.get("tcp_port")

    address: str | tuple[str, int]
    if socket_path and tcp_host:
        raise ConfigError(
            "shared_conversation: set socket_path OR tcp_host/tcp_port, not both"
        )
    if socket_path:
        address = os.path.expanduser(socket_path)
    elif tcp_host:
        try:
            port = int(str(tcp_port).strip())
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "shared_conversation: tcp_host requires an integer tcp_port"
            ) from exc
        address = (tcp_host, port)
    else:
        raise ConfigError(
            "shared_conversation: enabled but no socket_path or tcp_host is set"
        )

    token = _resolve_secret(
        block.get("token_ref"), field_name="shared_conversation.token_ref"
    )
    if isinstance(address, tuple) and address[0] not in {
        "127.0.0.1",
        "::1",
        "localhost",
    } and not token:
        # Same refusal the service applies at bind: a non-loopback control
        # surface without a token is an open conversation.
        raise ConfigError(
            "shared_conversation: a non-loopback tcp_host requires token_ref"
        )

    def _positive(name: str, default: float) -> float:
        raw = block.get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"shared_conversation.{name}: not a number") from exc
        if value <= 0:
            raise ConfigError(f"shared_conversation.{name}: must be > 0")
        return value

    limit_raw = block.get("transcript_limit", _DEFAULT_TRANSCRIPT_LIMIT)
    try:
        transcript_limit = int(limit_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("shared_conversation.transcript_limit: not an int") from exc
    if not 1 <= transcript_limit <= 500:
        # 500 is the service's own documented ceiling for `transcript`.
        raise ConfigError("shared_conversation.transcript_limit: must be 1..500")

    entrance = block.get("entrance")
    entrance = entrance.strip() if isinstance(entrance, str) and entrance.strip() else None

    return SharedConversationConfig(
        address=address,
        token=token,
        poll_interval=_positive("poll_interval", _DEFAULT_POLL_INTERVAL_S),
        connect_timeout=_positive("connect_timeout", _DEFAULT_CONNECT_TIMEOUT_S),
        transcript_limit=transcript_limit,
        entrance=entrance,
        label=str(block.get("label") or "Astra (shared)"),
    )


_config_lock = threading.Lock()
_config_cache: tuple[float, SharedConversationConfig | None, str | None] = (0.0, None, None)


def active_config(force: bool = False) -> SharedConversationConfig | None:
    """Return the live config for this profile, or ``None`` when disabled.

    Cached for :data:`_CONFIG_TTL_S` so the dispatch hot path costs a clock
    read for the overwhelmingly common disabled case.
    """
    global _config_cache
    now = time.monotonic()
    with _config_lock:
        stamp, cached, err = _config_cache
        if not force and cached is not None and (now - stamp) < _CONFIG_TTL_S:
            return cached
        if not force and cached is None and err is None and (now - stamp) < _CONFIG_TTL_S and stamp:
            return None
    try:
        from tui_gateway import server

        block = (server._load_cfg() or {}).get(CONFIG_KEY)
    except Exception:
        block = None
    error: str | None = None
    parsed: SharedConversationConfig | None = None
    try:
        parsed = parse_config(block)
    except ConfigError as exc:
        error = str(exc)
        logger.error("shared_conversation config rejected: %s", exc)
    with _config_lock:
        _config_cache = (time.monotonic(), parsed, error)
    return parsed


def config_error() -> str | None:
    """Last configuration error, if the block was present but unusable."""
    with _config_lock:
        return _config_cache[2]


def reset_config_cache() -> None:
    """Test seam: drop the cached config so the next read hits config.yaml."""
    global _config_cache
    with _config_lock:
        _config_cache = (0.0, None, None)


# ─────────────────────────── attachments ────────────────────────────


@dataclass
class Attachment:
    session_id: str
    entrance: str
    client: AttachClient
    transport: Any
    conversation_id: str
    cursor: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    # client_message_ids this client submitted, so the poller does not
    # re-render the user's own line back at them as remote traffic.
    own_message_ids: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Event frames are HELD until the client is known to hold its session id
    # (see `release`). Until then they queue here, in order.
    released: bool = False
    pending_frames: list[dict] = field(default_factory=list)
    release_timer: threading.Timer | None = None


_attachments: dict[str, Attachment] = {}
_attachments_lock = threading.Lock()


def attachments() -> dict[str, Attachment]:
    """Test seam: the live attachment table."""
    with _attachments_lock:
        return dict(_attachments)


def _new_session_id() -> str:
    # Same shape as the gateway's own ids (8 hex) so nothing downstream has to
    # special-case the length.
    return uuid.uuid4().hex[:8]


def _emit(attachment: Attachment, event: str, payload: dict | None = None) -> bool:
    """Write one event frame straight to this client's transport.

    The virtual session is deliberately absent from ``server._sessions`` (it
    owns no agent), so ``server.write_json``'s session lookup cannot route
    for us and a poller thread has no contextvar binding. Writing to the
    captured transport is therefore the only correct route — and it is also
    what keeps a Desktop WS client and a stdio TUI client independent.

    Frames produced before the attachment is RELEASED are queued rather than
    written: a client installs its session id only when it reads the
    ``session.create``/``session.resume`` response, and a frame that overtakes
    that response would arrive for a session the frontend has never heard of.
    See :func:`release`.
    """
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": event, "session_id": attachment.session_id},
    }
    if payload is not None:
        frame["params"]["payload"] = payload
    with attachment.lock:
        if not attachment.released:
            attachment.pending_frames.append(frame)
            return True
    return _write_frame(attachment, frame)


def _write_frame(attachment: Attachment, frame: dict) -> bool:
    try:
        from tui_gateway.event_replay import _stamp_event

        _stamp_event(frame)
    except Exception:
        pass
    try:
        return bool(attachment.transport.write(frame))
    except Exception:
        logger.debug("shared_conversation emit failed", exc_info=True)
        return False


def release(session_id: str) -> bool:
    """Flush held frames: the client demonstrably has its session id now.

    Called when a later RPC arrives naming this session (proof the frontend
    read the attach response), and by a bounded fallback timer so a client
    that says nothing more is not starved of live traffic.
    """
    with _attachments_lock:
        attachment = _attachments.get(session_id)
    if attachment is None:
        return False
    with attachment.lock:
        if attachment.released:
            return False
        attachment.released = True
        queued = attachment.pending_frames
        attachment.pending_frames = []
        timer = attachment.release_timer
        attachment.release_timer = None
    if timer is not None:
        timer.cancel()
    for frame in queued:
        if not _write_frame(attachment, frame):
            detach(session_id, reason="transport_closed")
            return True
    return True


def _message_text(message: dict) -> str:
    # ``body`` is the real column name in the service's ``conversation_messages``
    # table (see its store.transcript SELECT). The fallbacks are defensive only.
    for key in ("body", "text", "content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _message_entrance(message: dict) -> str:
    """The entrance a row came from, per the service's ``source`` convention.

    ``ControlAPI.submit`` writes ``source = "client:<entrance>"``; Discord
    intake writes its own source string. There is no separate ``entrance``
    column, so this prefix IS the provenance.
    """
    source = str(message.get("source") or "").strip()
    if source.startswith("client:"):
        return source[len("client:"):] or "client"
    return source


def _project_message(message: dict, *, own_entrance: str) -> dict | None:
    """Project one service transcript row onto a gateway transcript message.

    ``inbound`` is a human turn, ``outbound`` is Astra. A human turn that came
    from a DIFFERENT entrance keeps an explicit provenance prefix: the gateway
    protocol has no "another client typed this" frame, and quietly rendering
    it as this terminal's own input would be a lie about who is talking.
    """
    text = _message_text(message)
    if not text:
        return None
    work_id = message.get("work_id")
    if message.get("direction") == "outbound":
        projected: dict[str, Any] = {"role": "assistant", "text": text}
        if work_id:
            projected["display_metadata"] = {"shared_work_id": work_id}
        return projected
    entrance = _message_entrance(message)
    author = str(message.get("sender_label") or message.get("sender_id") or "").strip()
    metadata: dict[str, Any] = {"shared_entrance": entrance, "shared_author": author}
    if work_id:
        metadata["shared_work_id"] = work_id
    if entrance and entrance != own_entrance:
        prefix = f"[via {entrance}]" + (f" {author}:" if author else "")
        return {
            "role": "user",
            "text": f"{prefix} {text}",
            "display_metadata": metadata,
        }
    return {"role": "user", "text": text, "display_metadata": metadata}


def _session_info(attachment: Attachment, status: dict, ping: dict) -> dict:
    conversation = status.get("conversation") or {}
    owner = status.get("owner") or {}
    model = status.get("model")
    if isinstance(model, dict):
        model_name = str(model.get("model") or model.get("name") or "unknown")
        provider = model.get("provider")
    else:
        model_name = str(model or "unknown")
        provider = None
    info: dict[str, Any] = {
        "model": model_name,
        "tools": {},
        "skills": {},
        "version": str(ping.get("version") or ""),
        "profile_name": str(conversation.get("owner") or "astra"),
        "cwd": os.getcwd(),
        # Not part of SessionInfo in ui-tui's types, but extra keys are inert
        # there and give the Desktop/Workbench a truthful provenance block.
        "shared_conversation": {
            "attached": True,
            "entrance": attachment.entrance,
            "conversation_id": conversation.get("id"),
            "thread_ref": conversation.get("thread_ref"),
            "thread_state": conversation.get("thread_state"),
            "owner_pid": owner.get("pid"),
            "owner_heartbeat_stale": owner.get("heartbeat_stale"),
            "held_by_this_process": False,
            "service": ping.get("service"),
            "service_commit": ping.get("git_commit"),
            "protocol_version": ping.get("protocol_version"),
            "note": (
                "This client is attached to a shared conversation owner. It "
                "runs no model and holds no lease."
            ),
        },
    }
    if provider:
        info["provider"] = provider
    return info


def _resolve_entrance(cfg: SharedConversationConfig, params: dict, transport: Any) -> str:
    explicit = params.get("entrance") or params.get("source")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if cfg.entrance:
        return cfg.entrance
    name = type(transport).__name__ if transport is not None else ""
    if name == "WSTransport":
        return "desktop"
    return "terminal"


def _connect(cfg: SharedConversationConfig) -> AttachClient:
    client = AttachClient(
        cfg.address, token=cfg.token, timeout=cfg.connect_timeout
    )
    try:
        client.connect()
    except OSError as exc:
        # A missing socket / refused connection is "the service is not there",
        # not an internal error — surface it as the unavailable path so the
        # client gets an actionable message instead of a bare errno.
        raise AttachError(f"cannot reach {cfg.address!r}: {exc}") from exc
    return client


# ─────────────────────────── the poller ─────────────────────────────


def _poll_loop(attachment: Attachment, cfg: SharedConversationConfig) -> None:
    """Stream new transcript rows to this client until it goes away.

    Protocol v1 has no server push, so the contract is poll-with-cursor: a
    dropped socket cannot lose a message because the cursor is client-held
    (``docs/evo-service.md`` §3). A write failure means the client is gone —
    we detach, and the service is untouched.

    Service restart recovery: ``AttachClient.call`` closes a dead socket
    before raising, so the next poll reconnects. The cursor is NOT reset on
    reconnect — it is the safe resume point, and re-reading from it yields
    exactly the rows this client has not seen. Nothing is re-submitted on
    reconnect: a submit whose receipt was uncertain is a human decision, never
    an automatic replay.
    """
    backoff = cfg.poll_interval
    warned_unreachable = False
    while not attachment.stop.is_set():
        try:
            page = attachment.client.transcript(
                after_seq=attachment.cursor, limit=cfg.transcript_limit
            )
            backoff = cfg.poll_interval
            if warned_unreachable:
                _emit(
                    attachment,
                    "notification.show",
                    {
                        "key": "shared-conversation-link",
                        "kind": "transient",
                        "level": "info",
                        "text": "Shared conversation service reachable again.",
                        "ttl_ms": 4000,
                    },
                )
                warned_unreachable = False
        except AttachError as exc:
            if not warned_unreachable:
                warned_unreachable = True
                _emit(
                    attachment,
                    "notification.show",
                    {
                        "key": "shared-conversation-link",
                        "kind": "sticky",
                        "level": "warn",
                        "text": (
                            "Shared conversation service unreachable "
                            f"({exc}). This client is detached; the owner and "
                            "the conversation are unaffected."
                        ),
                    },
                )
            attachment.stop.wait(min(backoff, 10.0))
            backoff = min(backoff * 2, 10.0)
            continue
        except Exception:
            logger.debug("shared_conversation poll failed", exc_info=True)
            attachment.stop.wait(cfg.poll_interval)
            continue

        messages = page.get("messages") or []
        for message in messages:
            if attachment.stop.is_set():
                break
            seq = message.get("seq")
            if isinstance(seq, int) and seq > attachment.cursor:
                attachment.cursor = seq
            if not _render_message(attachment, message):
                # Peer gone — stop cleanly. The service keeps running.
                detach(attachment.session_id, reason="transport_closed")
                return
        next_cursor = page.get("next_after_seq")
        if isinstance(next_cursor, int) and next_cursor > attachment.cursor:
            attachment.cursor = next_cursor
        if len(messages) >= cfg.transcript_limit:
            # A full page means there is probably more waiting; don't sleep.
            continue
        attachment.stop.wait(cfg.poll_interval)


def _render_message(attachment: Attachment, message: dict) -> bool:
    """Emit one transcript row as gateway event frames. False = peer gone.

    Own-echo suppression keys on ``source_native_id``: the service stores the
    submitting client's ``client_message_id`` in that column
    (``ControlAPI.submit`` passes it as ``source_native_id``), so it is the
    only field that can identify this client's own line coming back.
    """
    native_id = message.get("source_native_id")
    with attachment.lock:
        own = bool(native_id) and native_id in attachment.own_message_ids
        if own:
            attachment.own_message_ids.discard(native_id)
    if own:
        # This client already rendered its own input locally when it typed it.
        return True
    projected = _project_message(message, own_entrance=attachment.entrance)
    if projected is None:
        return True
    text = projected["text"]
    if not _emit(attachment, "message.start"):
        return False
    if not _emit(attachment, "message.delta", {"text": text}):
        return False
    payload: dict[str, Any] = {"text": text}
    if projected["role"] != "assistant":
        # A human turn from another entrance is not this client's answer;
        # mark it so a renderer can style it and a log can tell them apart.
        payload["display_metadata"] = projected.get("display_metadata") or {}
        payload["display_metadata"]["shared_role"] = projected["role"]
    return _emit(attachment, "message.complete", payload)


# ───────────────────────── attach / detach ──────────────────────────


def attach(
    cfg: SharedConversationConfig,
    params: dict,
    transport: Any,
    *,
    session_id: str | None = None,
    replay: bool = True,
) -> tuple[Attachment, dict, dict, list[dict]]:
    """Open a client attachment and return (attachment, info, status, messages).

    The poller is NOT started here. A client only installs the session id when
    it reads the ``session.create`` / ``session.resume`` RESPONSE, so a live
    frame emitted before that response would arrive for a session the frontend
    does not yet know about — and a transcript larger than one page makes that
    race routine, not theoretical. The caller starts the poller with
    :func:`start_polling` after the response has been handed back.
    """
    entrance = _resolve_entrance(cfg, params, transport)
    client = _connect(cfg)
    ping = client.ping()
    status = client.status()
    conversation = (status.get("conversation") or {}).get("id") or ""
    attachment = Attachment(
        session_id=session_id or _new_session_id(),
        entrance=entrance,
        client=client,
        transport=transport,
        conversation_id=str(conversation),
    )

    messages: list[dict] = []
    cursor = 0
    if replay:
        # Drain the whole backlog, not one page: the cursor the poller starts
        # from must be the end of what the response already contains, or a
        # multi-page history would be re-emitted as "live" traffic.
        while True:
            page = client.transcript(after_seq=cursor, limit=cfg.transcript_limit)
            rows = page.get("messages") or []
            for message in rows:
                seq = message.get("seq")
                if isinstance(seq, int) and seq > cursor:
                    cursor = seq
                projected = _project_message(message, own_entrance=entrance)
                if projected is not None:
                    messages.append(projected)
            next_cursor = page.get("next_after_seq")
            if isinstance(next_cursor, int) and next_cursor > cursor:
                cursor = next_cursor
            if len(rows) < cfg.transcript_limit:
                break
    attachment.cursor = cursor

    with _attachments_lock:
        _attachments[attachment.session_id] = attachment

    return attachment, _session_info(attachment, status, ping), status, messages


def start_polling(attachment: Attachment, cfg: SharedConversationConfig) -> None:
    """Begin streaming live frames to this client. Idempotent.

    Called only after the attach RESPONSE has been produced, so the frontend
    has its session identity before the first event lands (see :func:`attach`).
    """
    if attachment.thread is not None:
        return
    thread = threading.Thread(
        target=_poll_loop,
        args=(attachment, cfg),
        name=f"shared-conversation-{attachment.session_id}",
        daemon=True,
    )
    attachment.thread = thread
    thread.start()


def detach(session_id: str, *, reason: str = "closed") -> bool:
    """Drop a client attachment. The service and its owner are untouched."""
    with _attachments_lock:
        attachment = _attachments.pop(session_id, None)
    if attachment is None:
        return False
    attachment.stop.set()
    with attachment.lock:
        timer = attachment.release_timer
        attachment.release_timer = None
        attachment.pending_frames = []
    if timer is not None:
        timer.cancel()
    try:
        attachment.client.close()
    except Exception:
        pass
    logger.info(
        "shared_conversation detached session=%s reason=%s", session_id, reason
    )
    return True


def detach_all(reason: str = "shutdown") -> int:
    with _attachments_lock:
        ids = list(_attachments)
    return sum(1 for sid in ids if detach(sid, reason=reason))


def detach_transport(transport: Any) -> int:
    """Drop every attachment bound to a transport (WS disconnect)."""
    with _attachments_lock:
        ids = [
            sid for sid, att in _attachments.items() if att.transport is transport
        ]
    return sum(1 for sid in ids if detach(sid, reason="transport_disconnect"))


# ─────────────────────────── RPC routing ────────────────────────────

# Handled here. Everything else falls through to the ordinary gateway
# handlers, which are read-only/UI concerns (completion, config, project tree)
# in this mode.
_ROUTED = {
    "session.create",
    "session.resume",
    "session.most_recent",
    "session.close",
    "session.status",
    "session.interrupt",
    "prompt.submit",
}

# Would spawn a second model runner or mutate an owner-owned conversation from
# a client. Refused with an explicit reason rather than silently falling
# through to the local agent path — which is exactly the bug this mode exists
# to prevent.
#
# ``session.interrupt`` is NOT here: the service grew an explicit ``stop``
# method (control_server.py ``METHODS["stop"] -> stop_work``), so the
# terminal/Desktop interrupt maps onto the owner's real stop rather than being
# blanket-refused. See :func:`_handle_interrupt`.
_REFUSED = {
    "prompt.background": "background turns belong to the shared owner",
    "session.branch": "a client cannot fork the owner's conversation",
    "session.compress": "compression is the owner's decision",
    "session.undo": "the owner owns transcript history",
    "session.steer": "protocol v1 has no steer method; use stop then resend",
    "session.delete": "a client cannot delete the shared conversation",
    "session.save": "the service persists the conversation itself",
}


def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, msg: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": msg}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def _lookup(params: dict) -> Attachment | None:
    sid = str(params.get("session_id") or "")
    if not sid:
        return None
    with _attachments_lock:
        return _attachments.get(sid)


def maybe_dispatch(
    rid: Any, method: str, params: dict, transport: Any
) -> dict | None:
    """The single hook ``server.dispatch`` calls.

    Returns a JSON-RPC response dict when shared mode handled the call, or
    ``None`` to let the ordinary gateway handlers run. ``None`` is the answer
    for every profile that has not opted in.
    """
    if method not in _ROUTED and method not in _REFUSED:
        return None
    cfg = active_config()
    if cfg is None:
        error = config_error()
        if error and method in _ROUTED:
            # Present-but-broken config must not fall back to a local model
            # runner: that is how you end up with two owners.
            return _err(rid, ERR_UNAVAILABLE, f"shared conversation disabled: {error}")
        return None

    if method in _REFUSED:
        return _err(
            rid,
            ERR_REFUSED_METHOD,
            f"{method} is not available in shared-conversation mode: "
            f"{_REFUSED[method]}",
        )

    try:
        if method in {"session.create", "session.resume", "session.most_recent"}:
            return _handle_attach(rid, method, params, transport, cfg)
        # Any other routed method naming a live session proves the client read
        # the attach response and holds the session id — release held frames.
        if params.get("session_id"):
            release(str(params["session_id"]))
        if method == "session.close":
            return _handle_close(rid, params)
        if method == "session.status":
            return _handle_status(rid, params, cfg)
        if method == "session.interrupt":
            return _handle_interrupt(rid, params)
        if method == "prompt.submit":
            return _handle_submit(rid, params, transport, cfg)
    except AttachError as exc:
        return _err(
            rid,
            ERR_UNAVAILABLE,
            f"shared conversation service unavailable: {exc}",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("shared_conversation handler failed method=%s", method)
        return _err(rid, ERR_UNAVAILABLE, f"shared conversation error: {exc}")
    return None


def _handle_attach(
    rid: Any,
    method: str,
    params: dict,
    transport: Any,
    cfg: SharedConversationConfig,
) -> dict:
    requested = str(params.get("session_id") or "")
    existing = _lookup(params) if requested else None
    if existing is not None and existing.transport is transport:
        # Re-attach on the same client: reuse the live attachment rather than
        # opening a second socket for the same window.
        status = existing.client.status()
        ping = existing.client.ping()
        page = existing.client.transcript(after_seq=0, limit=cfg.transcript_limit)
        messages = [
            projected
            for message in (page.get("messages") or [])
            if (projected := _project_message(message, own_entrance=existing.entrance))
        ]
        return _ok(
            rid,
            {
                "session_id": existing.session_id,
                "stored_session_id": existing.conversation_id,
                "message_count": len(messages),
                "messages": messages,
                "resumed": existing.conversation_id,
                "running": False,
                "status": "idle",
                "info": _session_info(existing, status, ping),
            },
        )

    attachment, info, status, messages = attach(
        cfg,
        params,
        transport,
        session_id=requested or None,
    )
    result = {
        "session_id": attachment.session_id,
        "stored_session_id": attachment.conversation_id,
        "message_count": len(messages),
        "messages": messages,
        "info": info,
    }
    if method != "session.create":
        result.update(
            {
                "resumed": attachment.conversation_id,
                "running": False,
                "status": "idle",
                "started_at": time.time(),
            }
        )
    owner = status.get("owner") or {}
    if owner.get("heartbeat_stale"):
        _emit(
            attachment,
            "notification.show",
            {
                "key": "shared-conversation-owner",
                "kind": "sticky",
                "level": "warn",
                "text": (
                    "Shared conversation owner heartbeat is stale — messages "
                    "will queue until it recovers."
                ),
            },
        )
    # Poll only now: the response below carries the session id the client
    # needs before any live frame can make sense to it, and frames stay held
    # until `release` (a later RPC on this session, or the fallback timer).
    start_polling(attachment, cfg)
    _arm_release_timer(attachment, cfg)
    return _ok(rid, result)


# A client that attaches and then says nothing (a passive viewer) would never
# trigger release-by-later-RPC. Flush after this many seconds so live traffic
# is delayed, never lost. Generous relative to a local RPC round trip.
_RELEASE_FALLBACK_S = 2.0


def _arm_release_timer(attachment: Attachment, cfg: SharedConversationConfig) -> None:
    timer = threading.Timer(
        _RELEASE_FALLBACK_S, lambda: release(attachment.session_id)
    )
    timer.daemon = True
    with attachment.lock:
        attachment.release_timer = timer
    timer.start()


def _handle_interrupt(rid: Any, params: dict) -> dict:
    """Map the terminal/Desktop interrupt onto the owner's real stop.

    The service exposes ``stop`` (``ControlAPI.stop_work``), which goes through
    the same archive-first accept path as a message, so a terminal stop is as
    durable and auditable as a Discord one. This is a DECLARED stop: no
    negation heuristic can talk it out of it. It is not a local cancel — this
    process has no turn to cancel.

    Idempotency: the client's own ``client_message_id`` is used when supplied,
    so a repeated interrupt is recognised as the same stop rather than filed
    twice. A duplicate returns ``applied: false`` and says so.
    """
    attachment = _lookup(params)
    if attachment is None:
        return _err(
            rid,
            ERR_UNAVAILABLE,
            "no shared-conversation attachment for this session",
        )
    client_message_id = str(params.get("client_message_id") or uuid.uuid4().hex)
    reason = str(params.get("reason") or "stop").strip() or "stop"
    result = attachment.client.call(
        "stop",
        client_message_id=client_message_id,
        entrance=attachment.entrance,
        reason=reason,
    ) or {}
    applied = bool(result.get("applied"))
    _emit(
        attachment,
        "status.update",
        {
            "kind": "status",
            "text": (
                "Stop declared to the shared owner."
                if applied
                else "This stop was already delivered to the owner."
            ),
        },
    )
    return _ok(
        rid,
        {
            "interrupted": applied,
            "shared_conversation": {
                "stopped": applied,
                "duplicate": bool(result.get("duplicate")),
                "message_id": result.get("message_id"),
                "hold_state": result.get("hold_state") or result.get("state"),
                "client_message_id": client_message_id,
                "note": (
                    "Declared a stop to the shared owner via the service 'stop' "
                    "method. This receipt records the declaration, not the "
                    "owner's completion of it."
                ),
            },
        },
    )


def _handle_close(rid: Any, params: dict) -> dict:
    sid = str(params.get("session_id") or "")
    closed = detach(sid, reason="session.close")
    return _ok(
        rid,
        {
            "closed": closed,
            "shared_conversation": {
                "detached": closed,
                "service_stopped": False,
                "note": "Closing a client never stops the shared service.",
            },
        },
    )


def _handle_status(rid: Any, params: dict, cfg: SharedConversationConfig) -> dict:
    attachment = _lookup(params)
    if attachment is None:
        client = _connect(cfg)
        try:
            status = client.status()
            ping = client.ping()
        finally:
            client.close()
        return _ok(
            rid,
            {
                "attached": False,
                "status": "idle",
                "running": False,
                "shared_conversation": {"service": ping, "service_status": status},
            },
        )
    status = attachment.client.status()
    ping = attachment.client.ping()
    return _ok(
        rid,
        {
            "attached": True,
            "session_id": attachment.session_id,
            "status": "idle",
            "running": False,
            "info": _session_info(attachment, status, ping),
            "shared_conversation": {"service": ping, "service_status": status},
        },
    )


def _handle_submit(
    rid: Any, params: dict, transport: Any, cfg: SharedConversationConfig
) -> dict:
    attachment = _lookup(params)
    if attachment is None:
        return _err(
            rid,
            ERR_UNAVAILABLE,
            "no shared-conversation attachment for this session; "
            "call session.create/session.resume first",
        )
    # Keep events flowing to whichever socket this client is on now.
    attachment.transport = transport or attachment.transport

    text = params.get("text")
    if not isinstance(text, str) or not text.strip():
        return _err(rid, -32602, "prompt.submit: text must be a non-empty string")

    # Attachment/image input: MISSING SEAM, reported rather than dropped.
    #
    # ``ControlAPI.submit`` (astra-evo-service control_server.py) accepts only
    # body / client_message_id / entrance / enqueue / work_key / sender_*.
    # There is no image, attachment or media parameter anywhere in service
    # protocol v1, and ``docs/evo-service.md`` §7 independently records that
    # the installed Codex adapter renders images as ``[image attached]`` so an
    # image does not reach the model at all. A bridge that quietly sent the
    # text and discarded the image would make a client believe an attachment
    # was delivered. Until the service grows an attachment parameter (image
    # API work is task t_2c7e41b6), we refuse the send and say exactly why.
    images = params.get("images") or params.get("attachments")
    if isinstance(images, list) and images:
        return _err(
            rid,
            ERR_REFUSED_METHOD,
            "shared-conversation mode cannot carry attachments: service "
            "protocol v1 'submit' has no attachment/image parameter (body, "
            "client_message_id, entrance, enqueue, work_key only). Send the "
            "message as text, or attach via an entrance the service supports.",
            {"missing_seam": "submit.attachments", "protocol_version": 1},
        )

    # Idempotency key: a client retry MUST reuse it, a genuinely new message
    # MUST NOT (docs/evo-service.md §3).
    #
    # Where it comes from, and what happens when it does not: a frontend that
    # supplies ``client_message_id`` gets true retry idempotency — resending
    # the same key after a dropped reply is recognised as the SAME message and
    # returns ``duplicate: true`` without queueing a second turn. A frontend
    # that omits it (today: the stock TUI/Desktop composer, which has no such
    # field) gets a fresh UUID per RPC, so a user-initiated resend is treated
    # as a genuinely new message — which is correct for new text and is the
    # only safe default for text we cannot prove is a retry.
    #
    # This bridge NEVER auto-retries a submit. If the receipt is uncertain
    # (socket error after the frame was written, unknown outcome), the error
    # is surfaced and the decision to resend is the human's — an automatic
    # resend under a fresh key would duplicate a real message, and under the
    # same key would silently swallow a genuine second send.
    client_message_id = str(
        params.get("client_message_id") or uuid.uuid4().hex
    )
    with attachment.lock:
        attachment.own_message_ids.add(client_message_id)

    receipt = attachment.client.submit(
        text,
        entrance=attachment.entrance,
        client_message_id=client_message_id,
        enqueue=True,
    )

    accepted = bool(receipt.get("accepted"))
    duplicate = bool(receipt.get("duplicate"))
    shared = {
        "submitted": True,
        "entrance": attachment.entrance,
        "client_message_id": client_message_id,
        "message_id": receipt.get("message_id"),
        "turn_id": receipt.get("turn_id"),
        "accepted": accepted,
        "duplicate": duplicate,
        "policy": receipt.get("policy"),
        "chair_retained": receipt.get("chair_retained"),
        "retention_reasons": receipt.get("retention_reasons"),
        # The wording matters: this client neither ran a turn nor delivered
        # anything. It handed the message to the owner.
        "note": (
            "Queued with the shared conversation owner. This receipt is not "
            "proof of execution or delivery."
        ),
    }

    if duplicate:
        _emit(
            attachment,
            "status.update",
            {
                "kind": "status",
                "text": "Already received by the shared owner (duplicate key).",
            },
        )
        return _ok(rid, {"status": "idle", "shared_conversation": shared})

    if not accepted:
        _emit(
            attachment,
            "status.update",
            {
                "kind": "status",
                "text": "The shared owner did not accept this message.",
            },
        )
        return _ok(
            rid,
            {
                "status": "idle",
                "shared_conversation": shared,
            },
        )

    _emit(
        attachment,
        "status.update",
        {"kind": "status", "text": "Queued with the shared Astra owner…"},
    )
    # "streaming" is the frontends' busy latch; the reply arrives as
    # message.start/delta/complete from the poller when the OWNER runs the
    # turn. We never call run_turn.
    return _ok(rid, {"status": "streaming", "shared_conversation": shared})


__all__ = [
    "AttachClient",
    "AttachError",
    "Attachment",
    "ConfigError",
    "SharedConversationConfig",
    "active_config",
    "attach",
    "attachments",
    "config_error",
    "detach",
    "detach_all",
    "detach_transport",
    "maybe_dispatch",
    "parse_config",
    "release",
    "reset_config_cache",
    "start_polling",
]
