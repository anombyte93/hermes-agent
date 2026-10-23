"""Known-answer tests for the continuity parser boundary and runtime fidelity.

Two defects are locked in here.

**1. Present-but-unusable was indistinguishable from absent.**
``CodexThreadRecord.from_dict`` returned ``None`` for a missing key, an
unsupported version, AND a malformed blob. The caller reads ``None`` as "no
prior conversation" and calls ``thread/start``, so a corrupt or
future-versioned record silently abandoned a live conversation. Only two
states may now yield ``None``: the key is genuinely absent, or it holds an
explicit tombstone. Everything else raises with the original bytes attached.

**2. Model and permissions were labels, not runtime.**
``intended_model`` was recorded but never sent; ``thread/start`` carried
``cwd`` only. A thread whose record said ``read-only`` was observed running
sandbox_type=workspace-write / approval_policy=on-request (hermes-agent#41).
The request now carries the real ``model`` / ``sandbox`` / ``approvalPolicy``
fields from this codex build's schema, and codex's own response must confirm
them before the record can claim they were applied.

Also covers image input (previously collapsed to "[image attached]") and the
close-ordering rule: ownership is released only after the codex subprocess is
provably reaped.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from agent.transports.codex_app_server_session import (
    CodexAppServerSession,
    CodexUnsupportedPermissionProfile,
    _build_turn_input,
    _permission_params,
    _verify_runtime_fidelity,
)
from agent.transports.codex_thread_continuity import (
    CodexThreadContinuity,
    CodexThreadRecord,
    CodexThreadRecordUnusable,
    CodexThreadRuntimeFidelityError,
    SessionModelConfigThreadStore,
    THREAD_RECORD_KEY,
    TOMBSTONE_KEY,
)
from hermes_state import SessionDB


CWD = "/tmp"


class FidelityClient:
    """Fake app-server that answers thread/start|resume like the real schema.

    ``reply_overrides`` lets a test make codex answer with something OTHER
    than what was requested — that is how the mismatch refusal is proved to
    fire rather than merely being asserted about.
    """

    def __init__(self, *, reply_overrides: Optional[dict] = None,
                 omit: tuple = (), thread_id: str = "thread-fid-001",
                 known_threads: Optional[set] = None) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.reply_overrides = reply_overrides or {}
        self.omit = set(omit)
        self.thread_id = thread_id
        self.known_threads = set(known_threads or ())
        self.closed = False
        self.alive_after_close = False

    def initialize(self, **kwargs):
        return {"userAgent": "fake/0.0.0"}

    def _attach_reply(self, params: dict, tid: str) -> dict:
        sandbox_mode = params.get("sandbox")
        policy_type = {
            "read-only": "readOnly",
            "workspace-write": "workspaceWrite",
            "danger-full-access": "dangerFullAccess",
        }.get(sandbox_mode)
        reply: dict[str, Any] = {
            "thread": {"id": tid},
            "model": params.get("model") or "gpt-5-codex",
            "approvalPolicy": params.get("approvalPolicy") or "on-request",
        }
        if policy_type:
            reply["sandbox"] = {"type": policy_type}
        reply.update(self.reply_overrides)
        for key in self.omit:
            reply.pop(key, None)
        return reply

    def request(self, method: str, params: Optional[dict] = None,
                timeout: float = 30.0):
        params = params or {}
        self.requests.append((method, dict(params)))
        if method == "thread/start":
            self.known_threads.add(self.thread_id)
            return self._attach_reply(params, self.thread_id)
        if method == "thread/resume":
            return self._attach_reply(params, params.get("threadId"))
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    def notify(self, *a, **kw):
        pass

    def take_notification(self, timeout: float = 0.0):
        return None

    def take_server_request(self, timeout: float = 0.0):
        return None

    def close(self):
        self.closed = True

    def is_alive(self) -> bool:
        if self.closed:
            return self.alive_after_close
        return True

    def stderr_tail(self, n: int = 20):
        return []

    def params_for(self, method: str) -> dict:
        for m, p in self.requests:
            if m == method:
                return p
        raise AssertionError(f"{method} never requested; saw {self.requests}")


@pytest.fixture()
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def make_continuity(db, session_id, *, permission_profile="read-only",
                    model="gpt-6-astra"):
    db.create_session(session_id, source="cli")
    return CodexThreadContinuity(
        SessionModelConfigThreadStore(db, session_id),
        cwd=CWD,
        permission_profile=permission_profile,
        model=model,
    )


def make_session(client, continuity=None, **kwargs):
    kwargs.setdefault("permission_profile", "read-only")
    kwargs.setdefault("model", "gpt-6-astra")
    return CodexAppServerSession(
        cwd=CWD,
        client_factory=lambda **kw: client,
        continuity=continuity,
        **kwargs,
    )


# --------------------------------------------------------------------------
# 1. parser boundary: missing / valid / unsupported / corrupt / cleared
# --------------------------------------------------------------------------

class TestParserKnownAnswers:
    def test_missing_is_none(self):
        """Absent key is the ONLY silent 'no conversation' state."""
        assert CodexThreadRecord.from_dict(None) is None

    def test_valid_v1_parses(self):
        record = CodexThreadRecord.from_dict(
            {"thread_id": "abc", "version": 1, "cwd": CWD}
        )
        assert record is not None
        assert record.thread_id == "abc"

    def test_unsupported_version_refuses_loudly(self):
        """Was: returns None → silent thread/start on a live conversation."""
        raw = {"thread_id": "abc", "version": 999}
        with pytest.raises(CodexThreadRecordUnusable) as exc:
            CodexThreadRecord.from_dict(raw)
        assert "999" in str(exc.value)
        # original bytes preserved on the refusal, never rewritten
        assert exc.value.raw is raw

    def test_malformed_empty_object_refuses_loudly(self):
        with pytest.raises(CodexThreadRecordUnusable):
            CodexThreadRecord.from_dict({})

    def test_malformed_non_object_refuses_loudly(self):
        with pytest.raises(CodexThreadRecordUnusable):
            CodexThreadRecord.from_dict("not-a-record")

    def test_blank_thread_id_refuses_loudly(self):
        with pytest.raises(CodexThreadRecordUnusable):
            CodexThreadRecord.from_dict({"thread_id": "   ", "version": 1})

    def test_explicit_tombstone_is_a_legitimate_fresh_start(self):
        assert CodexThreadRecord.from_dict({TOMBSTONE_KEY: True}) is None


class TestStoreBoundary:
    def test_unsupported_record_refuses_before_any_codex_rpc(self, db):
        """The refusal must land BEFORE thread/start, or the damage is done."""
        continuity = make_continuity(db, "s-unsupported")
        db.patch_session_model_config(
            "s-unsupported",
            {THREAD_RECORD_KEY: {"thread_id": "live-thread", "version": 999}},
        )
        client = FidelityClient()
        session = make_session(client, continuity)
        with pytest.raises(CodexThreadRecordUnusable):
            session.ensure_started()
        assert client.requests == [], (
            "a codex RPC was issued despite an unusable record"
        )

    def test_refusal_preserves_the_original_bytes(self, db):
        continuity = make_continuity(db, "s-preserve")
        payload = {"thread_id": "live-thread", "version": 999, "cwd": CWD}
        db.patch_session_model_config("s-preserve", {THREAD_RECORD_KEY: payload})
        with pytest.raises(CodexThreadRecordUnusable):
            continuity.load_resumable()
        after = db.get_session_model_config_value("s-preserve", THREAD_RECORD_KEY)
        assert after == payload, "the refusal rewrote the evidence"

    def test_explicit_clear_writes_a_tombstone_and_then_starts_fresh(self, db):
        """Explicit clear is the sanctioned route back to a fresh thread."""
        continuity = make_continuity(db, "s-clear")
        client = FidelityClient()
        session = make_session(client, continuity)
        first = session.ensure_started()
        session.close()

        store = SessionModelConfigThreadStore(db, "s-clear")
        store.clear(reason="operator ended the conversation")
        raw = db.get_session_model_config_value("s-clear", THREAD_RECORD_KEY)
        assert raw[TOMBSTONE_KEY] is True
        assert raw["cleared_reason"] == "operator ended the conversation"
        # the superseded record is kept as evidence, not deleted
        assert raw["superseded"]["thread_id"] == first

        client2 = FidelityClient(thread_id="thread-fid-002")
        session2 = make_session(client2, make_continuity_for(db, "s-clear"))
        second = session2.ensure_started()
        assert second == "thread-fid-002"
        assert not session2.resumed
        assert "thread/start" in [m for m, _ in client2.requests]


def make_continuity_for(db, session_id, **kwargs):
    """Continuity for a session row that already exists."""
    return CodexThreadContinuity(
        SessionModelConfigThreadStore(db, session_id),
        cwd=CWD,
        permission_profile=kwargs.get("permission_profile", "read-only"),
        model=kwargs.get("model", "gpt-6-astra"),
    )


# --------------------------------------------------------------------------
# 2. runtime fidelity: model + permissions actually sent and confirmed
# --------------------------------------------------------------------------

class TestPermissionProtocolMapping:
    @pytest.mark.parametrize("profile,sandbox,approval", [
        ("read-only", "read-only", "never"),
        ("read-only-with-approval", "read-only", "on-request"),
        ("workspace-write", "workspace-write", "on-request"),
        ("full-access", "danger-full-access", "never"),
    ])
    def test_known_profiles_map_to_schema_values(self, profile, sandbox, approval):
        params = _permission_params(profile)
        assert params == {"sandbox": sandbox, "approvalPolicy": approval}

    def test_empty_profile_sends_no_override(self):
        """No stated preference → codex's configured default, not a guess."""
        assert _permission_params("") == {}

    def test_unsupported_profile_refuses_loudly(self):
        """Compatibility is retained as a REFUSAL, not a silent fallback."""
        with pytest.raises(CodexUnsupportedPermissionProfile) as exc:
            _permission_params("some-future-profile")
        assert "some-future-profile" in str(exc.value)


class TestRuntimeFidelityVerification:
    def test_model_and_permissions_are_sent_on_thread_start(self, db):
        client = FidelityClient()
        session = make_session(client, make_continuity(db, "s-fid"))
        session.ensure_started()
        params = client.params_for("thread/start")
        assert params["model"] == "gpt-6-astra"
        assert params["sandbox"] == "read-only"
        assert params["approvalPolicy"] == "never"

    def test_model_and_permissions_are_sent_on_resume(self, db):
        continuity = make_continuity(db, "s-fid-resume")
        client = FidelityClient()
        session = make_session(client, continuity)
        tid = session.ensure_started()
        session.close()

        client2 = FidelityClient(known_threads={tid})
        session2 = make_session(client2, make_continuity_for(db, "s-fid-resume"))
        assert session2.ensure_started() == tid
        params = client2.params_for("thread/resume")
        assert params["threadId"] == tid
        assert params["model"] == "gpt-6-astra"
        assert params["sandbox"] == "read-only"
        assert params["approvalPolicy"] == "never"

    def test_confirmed_runtime_is_recorded_as_applied(self, db):
        client = FidelityClient()
        session = make_session(client, make_continuity(db, "s-applied"))
        session.ensure_started()
        record = CodexThreadRecord.from_dict(
            db.get_session_model_config_value("s-applied", THREAD_RECORD_KEY)
        )
        assert record.model == "gpt-6-astra"
        assert record.model_applied is True
        assert record.permissions_applied is True
        assert record.sandbox == "readOnly"
        assert record.approval_policy == "never"

    def test_wrong_sandbox_in_response_is_refused(self, db):
        """The hermes-agent#41 observation, reproduced as a gate.

        Requested read-only, codex answers workspace-write / on-request —
        exactly what the saved rollout showed. Before this change nothing
        looked, and the record kept claiming read-only.
        """
        client = FidelityClient(reply_overrides={
            "sandbox": {"type": "workspaceWrite"},
            "approvalPolicy": "on-request",
        })
        session = make_session(client, make_continuity(db, "s-mismatch"))
        with pytest.raises(CodexThreadRuntimeFidelityError) as exc:
            session.ensure_started()
        assert "workspaceWrite" in str(exc.value)

    def test_wrong_model_in_response_is_refused(self, db):
        client = FidelityClient(reply_overrides={"model": "gpt-5-codex"})
        session = make_session(client, make_continuity(db, "s-model-mismatch"))
        with pytest.raises(CodexThreadRuntimeFidelityError) as exc:
            session.ensure_started()
        assert "gpt-5-codex" in str(exc.value)

    def test_silent_response_records_unconfirmed_not_applied(self, db):
        """Silence is not proof. It must never set the applied flags."""
        client = FidelityClient(omit=("model", "sandbox", "approvalPolicy"))
        session = make_session(client, make_continuity(db, "s-silent"))
        session.ensure_started()
        record = CodexThreadRecord.from_dict(
            db.get_session_model_config_value("s-silent", THREAD_RECORD_KEY)
        )
        assert record.model_applied is False
        assert record.permissions_applied is False

    def test_verify_helper_returns_observed_facts(self):
        observed = _verify_runtime_fidelity(
            {"model": "gpt-6-astra", "sandbox": {"type": "readOnly"},
             "approvalPolicy": "never"},
            requested_model="gpt-6-astra",
            requested_permissions={"sandbox": "read-only",
                                   "approvalPolicy": "never"},
            op="thread/start",
        )
        assert observed["model_applied"] is True
        assert observed["permissions_applied"] is True
        assert observed["unconfirmed"] == []


# --------------------------------------------------------------------------
# 3. image input reaches codex instead of collapsing to a marker
# --------------------------------------------------------------------------

class TestTurnInputImages:
    def test_plain_string_is_unchanged(self):
        """Existing text callers must be byte-identical to before."""
        assert _build_turn_input("hello") == [{"type": "text", "text": "hello"}]

    def test_http_image_becomes_an_image_item(self):
        items = _build_turn_input([
            {"type": "text", "text": "what is this?"},
            {"type": "image_url",
             "image_url": {"url": "https://example.invalid/a.png"}},
        ])
        assert items == [
            {"type": "text", "text": "what is this?"},
            {"type": "image", "url": "https://example.invalid/a.png"},
        ]

    def test_data_url_image_is_carried_through(self):
        items = _build_turn_input([
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,AAAA"}},
        ])
        assert items == [{"type": "image", "url": "data:image/png;base64,AAAA"}]
        assert "[image attached]" not in str(items)

    def test_local_path_becomes_localImage(self):
        """codex reads the file itself; Hermes copies no bytes."""
        items = _build_turn_input([
            {"type": "image_url", "image_url": {"url": "file:///tmp/a.png"}},
        ])
        assert items == [{"type": "localImage", "path": "/tmp/a.png"}]

    def test_bare_absolute_path_becomes_localImage(self):
        items = _build_turn_input([
            {"type": "input_image", "url": "/home/x/shot.png"},
        ])
        assert items == [{"type": "localImage", "path": "/home/x/shot.png"}]

    def test_unaddressable_image_is_flagged_not_dropped(self):
        items = _build_turn_input([{"type": "image"}])
        assert items == [
            {"type": "text", "text": "[image attached, no usable URL]"}
        ]

    def test_turn_start_carries_the_image_item(self, db):
        client = FidelityClient()
        session = make_session(client, make_continuity(db, "s-img"))
        session.ensure_started()
        session.run_turn(
            user_input=[
                {"type": "text", "text": "describe"},
                {"type": "image_url",
                 "image_url": {"url": "file:///tmp/known.png"}},
            ],
            turn_timeout=0.2,
        )
        sent = client.params_for("turn/start")["input"]
        assert {"type": "localImage", "path": "/tmp/known.png"} in sent


# --------------------------------------------------------------------------
# 4. close ordering: ownership outlives the requested close, not the process
# --------------------------------------------------------------------------

class TestCloseOrdering:
    def test_client_is_closed_before_ownership_is_released(self, db):
        """Release-then-close let a successor acquire a live-owner thread."""
        continuity = make_continuity(db, "s-order")
        client = FidelityClient()
        session = make_session(client, continuity)
        session.ensure_started()

        order: list[str] = []
        real_close = client.close
        real_release = continuity.release

        def spy_close():
            order.append("client_closed")
            real_close()

        def spy_release():
            order.append("ownership_released")
            real_release()

        client.close = spy_close
        continuity.release = spy_release
        session.close()
        assert order == ["client_closed", "ownership_released"]

    def test_ownership_is_retained_when_the_subprocess_survives_close(self, db):
        """A close that does not reap codex must NOT hand the thread away."""
        continuity = make_continuity(db, "s-survivor")
        client = FidelityClient()
        client.alive_after_close = True  # ignored terminate/kill
        session = make_session(client, continuity)
        session.ensure_started()
        session.close()

        record = CodexThreadRecord.from_dict(
            db.get_session_model_config_value("s-survivor", THREAD_RECORD_KEY)
        )
        assert record.released_at == 0.0, (
            "ownership was released while the codex subprocess was still alive"
        )

    def test_ownership_is_retained_when_close_raises(self, db):
        continuity = make_continuity(db, "s-raiser")
        client = FidelityClient()

        def boom():
            client.closed = True
            raise RuntimeError("close timed out")

        session = make_session(client, continuity)
        session.ensure_started()
        client.close = boom
        client.alive_after_close = True
        session.close()

        record = CodexThreadRecord.from_dict(
            db.get_session_model_config_value("s-raiser", THREAD_RECORD_KEY)
        )
        assert record.released_at == 0.0

    def test_clean_close_does_release(self, db):
        """The retention rule must not strand a normal restart."""
        continuity = make_continuity(db, "s-clean")
        client = FidelityClient()
        session = make_session(client, continuity)
        session.ensure_started()
        session.close()
        record = CodexThreadRecord.from_dict(
            db.get_session_model_config_value("s-clean", THREAD_RECORD_KEY)
        )
        assert record.released_at > 0.0
