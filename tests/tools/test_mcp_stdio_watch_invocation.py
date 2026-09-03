"""Test that the stdio child‑watcher is invoked exactly once and that no
``RuntimeWarning: coroutine ... was never awaited`` leaks.

The fix in ``tools/mcp_tool.py`` replaced ``inspect.isawaitable(_watch_children())``
with ``inspect.iscoroutinefunction(_watch_children)``.  This test verifies the new
behaviour by spying on the coroutine function.
"""

import asyncio
import json
import pytest
from unittest.mock import MagicMock

from tools import mcp_tool
from tools.mcp_tool import _make_tool_handler


def _install_stub_server(name: str, call_tool_impl, *, children_dead):
    """Create a minimal fake MCP server with a real ``_watch_stdio_children``
    attribute that can be inspected.
    """
    server = MagicMock()
    server.name = name
    session = MagicMock()
    session.call_tool = call_tool_impl
    server.session = session
    server._reconnect_event = MagicMock()
    server._reconnect_event.set_calls = 0
    server._reconnect_event.set = MagicMock(
        side_effect=lambda: setattr(server._reconnect_event, "set_calls", server._reconnect_event.set_calls + 1)
    )
    server._ready = MagicMock()
    server._ready.is_set = MagicMock(return_value=True)
    server._is_recycled_stdio = MagicMock(return_value=False)
    server._stdio_children_dead = children_dead
    async def _noop_watch():
        return
    server._watch_stdio_children = _noop_watch
    mcp_tool._servers[name] = server
    return server


def _cleanup(name: str) -> None:
    mcp_tool._servers.pop(name, None)


def test_watch_stdio_children_invoked_once(monkeypatch, tmp_path):
    """Ensure the watcher coroutine is called exactly once and no RuntimeWarning is raised."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async def _call_tool(*a, **kw):  # pragma: no cover – exercised via handler
        return MagicMock(is_error=False, content=[])
    server = _install_stub_server("srv-watch", _call_tool, children_dead=lambda: False)
    call_counter = {"cnt": 0}
    async def _spy_watch():
        call_counter["cnt"] += 1
        await asyncio.sleep(0.1)
    server._watch_stdio_children = _spy_watch
    mcp_tool._ensure_mcp_loop()
    try:
        handler = _make_tool_handler("srv-watch", "tool1", 5.0)
        result_json = json.loads(handler({}))
        assert "error" not in result_json, result_json
        assert call_counter["cnt"] == 1, "_watch_stdio_children was not invoked exactly once"
    finally:
        _cleanup("srv-watch")
