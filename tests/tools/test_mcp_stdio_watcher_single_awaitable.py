"""Regression tests for the doubly-invoked stdio child watcher (issue #23).

The #81995 fast-fail race used to probe the child watcher with
``inspect.isawaitable(_watch_children())`` — which CALLS the watcher and
discards the returned coroutine — and then invoked the watcher a second
time for ``asyncio.ensure_future(_watch_children())``. Every real MCP tool
call therefore leaked one coroutine and logged::

    tools/mcp_tool.py:6189: RuntimeWarning: coroutine
    'MCPServerTask._watch_stdio_children' was never awaited

(80 occurrences in the installed Hermes Desktop log.) These tests pin the
property that matters: the watcher is invoked exactly once per tool call,
the single returned awaitable is the object raced against the RPC, and no
unawaited-coroutine RuntimeWarning escapes any path.

The never-awaited warning fires on garbage-collection of the leaked
coroutine, which happens on the background MCP loop thread; recording via
``warnings.catch_warnings`` swaps the global ``showwarning``/filters, so
warnings raised on that thread are captured here exactly as pytest's own
warning plugin captures them. Asserting the recorded list is empty is the
deterministic warning-as-error form: escalation via ``simplefilter("error")``
would raise inside ``coroutine.__del__`` on the loop thread and only print
"Exception ignored", never failing the test.
"""

import asyncio
import gc
import json
import threading
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("mcp")


def _install_stub_server(mcp_tool_module, name, call_tool_impl):
    """Fake MCP server with real-bool stdio liveness (mirrors
    tests/tools/test_mcp_stdio_fastfail_reconnect.py)."""
    server = MagicMock()
    server.name = name
    session = MagicMock()
    session.call_tool = call_tool_impl
    server.session = session

    ready_flag = threading.Event()
    ready_flag.set()

    class _ReconnectAdapter:
        def __init__(self):
            self.set_calls = 0

        def set(self):
            self.set_calls += 1

    server._reconnect_event = _ReconnectAdapter()
    server._ready = ready_flag
    server._is_recycled_stdio.return_value = False
    # Real-bool liveness: alive for the whole call in these tests.
    server._stdio_children_dead = lambda: False

    mcp_tool_module._servers[name] = server
    mcp_tool_module._server_error_counts.pop(name, None)
    if hasattr(mcp_tool_module, "_server_breaker_opened_at"):
        mcp_tool_module._server_breaker_opened_at.pop(name, None)
    return server


def _cleanup(mcp_tool_module, name):
    mcp_tool_module._servers.pop(name, None)
    mcp_tool_module._server_error_counts.pop(name, None)
    if hasattr(mcp_tool_module, "_server_breaker_opened_at"):
        mcp_tool_module._server_breaker_opened_at.pop(name, None)


def _run_handler_capturing_warnings(handler, args):
    """Run the sync handler with every RuntimeWarning recorded (from any
    thread) and force a GC sweep so a leaked coroutine finalizes — and
    warns — inside the capture window. A full sweep BEFORE the window
    discards earlier tests' cycle-held leaks so each test owns only its
    own."""
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        result = handler(args)
        gc.collect()
    return result, [str(w.message) for w in caught]


def _unawaited(messages):
    return [m for m in messages if "was never awaited" in m]


class _CountingWatcher:
    """Real coroutine-function watcher that records how many times it was
    CALLED, hands out the exact coroutine objects it created, and lets the
    test observe whether the body actually ran (i.e. the coroutine was
    scheduled, not just constructed)."""

    def __init__(self, mode):
        # mode "hang": await forever (success path — RPC wins, watcher
        #              cancelled in the finally cleanup).
        # mode "return": resolve immediately (mid-call death path —
        #              watcher wins the race, RPC cancelled).
        self.mode = mode
        self.calls = 0
        self.created = []  # coroutine objects handed out
        self.started = threading.Event()

    def __call__(self):
        self.calls += 1
        coro = self._run()
        self.created.append(coro)
        return coro

    async def _run(self):
        self.started.set()
        if self.mode == "hang":
            await asyncio.sleep(60)
        # "return": fall through and resolve.


def test_success_path_single_watcher_invocation(monkeypatch, tmp_path):
    """Real watcher + successful RPC: the watcher is invoked exactly once,
    its one coroutine is the object raced against the RPC (the body ran,
    so it was scheduled — not constructed and abandoned), and no
    never-awaited RuntimeWarning is emitted."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    async def _call_tool(*a, **kw):
        return SimpleNamespace(
            is_error=False, content=[], structured_content=None, meta=None
        )

    server = _install_stub_server(mcp_tool, "srv-once", _call_tool)
    watcher = _CountingWatcher(mode="hang")
    server._watch_stdio_children = watcher

    mcp_tool._ensure_mcp_loop()
    try:
        handler = _make_tool_handler("srv-once", "tool1", 10.0)
        result, messages = _run_handler_capturing_warnings(handler, {})
        parsed = json.loads(result)
        assert "error" not in parsed, parsed

        # Exactly one invocation: the awaitability probe must not call the
        # watcher a second time (#23 — the bug called it twice).
        assert watcher.calls == 1, (
            f"watcher invoked {watcher.calls} times; expected exactly 1"
        )
        # Exactly one coroutine was ever created...
        assert len(watcher.created) == 1
        # ...and its body ran, so the raced object is the created one —
        # not a second, leaked coroutine.
        assert watcher.started.is_set(), (
            "the single watcher coroutine was never scheduled — it was "
            "constructed and abandoned"
        )
        # No leaked awaitable: nothing warned "never awaited".
        leaked = _unawaited(messages)
        assert not leaked, f"unawaited coroutine(s) leaked: {leaked}"
    finally:
        _cleanup(mcp_tool, "srv-once")


def test_midcall_death_path_single_watcher_invocation(monkeypatch, tmp_path):
    """Watcher wins the race (children die mid-call): still exactly one
    invocation, one coroutine, fast-fail error + reconnect signal
    (behaviour of #95626 preserved), and no never-awaited warning."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    async def _hanging_call(*a, **kw):
        await asyncio.sleep(30)

    server = _install_stub_server(mcp_tool, "srv-mid", _hanging_call)
    watcher = _CountingWatcher(mode="return")
    server._watch_stdio_children = watcher

    mcp_tool._ensure_mcp_loop()
    try:
        handler = _make_tool_handler("srv-mid", "tool1", 10.0)
        result, messages = _run_handler_capturing_warnings(handler, {})
        parsed = json.loads(result)
        assert "error" in parsed, parsed
        assert "exited mid-call" in parsed["error"], parsed
        assert server._reconnect_event.set_calls == 1

        assert watcher.calls == 1, (
            f"watcher invoked {watcher.calls} times; expected exactly 1"
        )
        assert len(watcher.created) == 1
        assert watcher.started.is_set()
        leaked = _unawaited(messages)
        assert not leaked, f"unawaited coroutine(s) leaked: {leaked}"
    finally:
        _cleanup(mcp_tool, "srv-mid")


def test_magicmock_watcher_fallback_unchanged(monkeypatch, tmp_path):
    """Stubbed sessions (MagicMock in tests): the auto-created Mock watcher
    attribute returns a non-awaitable, so the pre-#81995 plain-await
    fallback must hold — call succeeds, no watcher race, no warning."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    async def _call_tool(*a, **kw):
        return SimpleNamespace(
            is_error=False, content=[], structured_content=None, meta=None
        )

    # No explicit _watch_stdio_children: MagicMock auto-creates one whose
    # return value is a Mock — not awaitable.
    server = _install_stub_server(mcp_tool, "srv-mock", _call_tool)

    mcp_tool._ensure_mcp_loop()
    try:
        handler = _make_tool_handler("srv-mock", "tool1", 10.0)
        result, messages = _run_handler_capturing_warnings(handler, {})
        parsed = json.loads(result)
        assert "error" not in parsed, parsed
        leaked = _unawaited(messages)
        assert not leaked, f"unawaited coroutine(s) leaked: {leaked}"
    finally:
        _cleanup(mcp_tool, "srv-mock")
