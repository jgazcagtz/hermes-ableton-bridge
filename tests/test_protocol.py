"""Hermes-Ableton bridge — tests for the JSON message protocol.

Validates message envelopes and the bridge server's protocol handling (auth,
response routing, state caching) by booting a real ws_server + mock_client
in-process.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest
import websockets

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server import ws_server  # noqa: E402
from tests.mock_client import run as mock_run  # noqa: E402

TOKEN = "test-token-123"


async def _start_bridge(port_ws, port_http):
    cfg = {
        "ws_host": "127.0.0.1", "ws_port": port_ws,
        "http_host": "127.0.0.1", "http_port": port_http,
        "auth_token": TOKEN, "command_timeout": 5.0,
        "state_interval": 0.3, "log_level": "WARNING",
        "ssl": {"enabled": False},
    }
    control = await ws_server.start_servers(cfg)
    return control


async def _start_mock(port_ws):
    return asyncio.create_task(mock_run("127.0.0.1", port_ws, TOKEN, 0.4, None))


async def _wait_connected(http_port, timeout=8):
    import aiohttp
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as session:
        while time.time() < deadline:
            try:
                async with session.get(f"http://127.0.0.1:{http_port}/status",
                                       timeout=aiohttp.ClientTimeout(total=2)) as r:
                    body = await r.json()
                    if body.get("ableton_connected"):
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.15)
    return False


async def _http_post(http_port, action, params=None):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{http_port}/command",
                                json={"action": action, "params": params or {}},
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
            return await r.json()


async def _http_get(http_port, path):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{http_port}{path}",
                               timeout=aiohttp.ClientTimeout(total=2)) as r:
            return await r.json()


# --------------------------------------------------------------------------- #
#  Pure envelope tests
# --------------------------------------------------------------------------- #
def test_command_envelope_shape():
    msg = {"id": "abc", "type": "command", "action": "play",
           "params": {}, "timestamp": int(time.time())}
    for key in ("id", "type", "action", "params", "timestamp"):
        assert key in msg
    assert msg["type"] == "command"


def test_response_envelope_ok():
    msg = {"id": "abc", "type": "response", "status": "ok",
           "data": {"playing": True}, "error": None, "timestamp": int(time.time())}
    assert msg["status"] in ("ok", "error") and msg["error"] is None


def test_response_envelope_error():
    msg = {"id": "abc", "type": "response", "status": "error",
           "data": {}, "error": "boom", "timestamp": int(time.time())}
    assert msg["status"] == "error" and msg["error"] == "boom"


def test_state_envelope_shape():
    msg = {"type": "state", "data": {"tempo": 120, "playing": False,
            "tracks": [], "scenes": []}, "timestamp": int(time.time())}
    assert msg["type"] == "state" and "tempo" in msg["data"]


# --------------------------------------------------------------------------- #
#  Live server + mock client tests
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bad_token_rejected():
    control = await _start_bridge(8180, 8181)
    try:
        async with websockets.connect("ws://127.0.0.1:8180") as ws:
            await ws.send(json.dumps({"auth": "wrong"}))
            resp = json.loads(await ws.recv())
            assert resp["status"] == "error"
    finally:
        await control["stop"]()


@pytest.mark.asyncio
async def test_command_roundtrip():
    control = await _start_bridge(8182, 8183)
    mock_task = await _start_mock(8182)
    try:
        assert await _wait_connected(8183), "mock client did not connect"
        resp = await _http_post(8183, "set_tempo", {"tempo": 140})
        assert resp["status"] == "ok"
        assert resp["data"]["tempo"] == 140
    finally:
        mock_task.cancel()
        await control["stop"]()


@pytest.mark.asyncio
async def test_state_cached_on_bridge():
    control = await _start_bridge(8184, 8185)
    mock_task = await _start_mock(8184)
    try:
        assert await _wait_connected(8185)
        await asyncio.sleep(1.0)  # let state reports arrive
        st = await _http_get(8185, "/state")
        assert "state" in st and "tempo" in st["state"]
    finally:
        mock_task.cancel()
        await control["stop"]()


@pytest.mark.asyncio
async def test_command_error_propagates():
    control = await _start_bridge(8186, 8187)
    mock_task = await _start_mock(8186)
    try:
        assert await _wait_connected(8187)
        # unknown action -> mock returns error -> bridge returns 500
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:8187/command",
                                    json={"action": "does_not_exist", "params": {}},
                                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                assert r.status == 500
    finally:
        mock_task.cancel()
        await control["stop"]()
