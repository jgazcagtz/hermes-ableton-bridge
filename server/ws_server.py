#!/usr/bin/env python3
"""
Hermes-Ableton Bridge — WebSocket Server
=========================================

Runs on the VPS (Linux). Bridges the Hermes agent (localhost HTTP, port 8081)
with the Max for Live device inside Ableton on Windows (WebSocket, port 8080).

  Hermes Agent  --HTTP:8081-->  ws_server  --WS:8080-->  Ableton (M4L)
                       <--              <--              <--

Only ONE Ableton WebSocket connection is accepted at a time (outbound from
Windows, so no inbound NAT/firewall poking on the Windows side). Multiple
Hermes agent HTTP clients are supported concurrently.

Usage:
    python ws_server.py
    python ws_server.py --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import ssl
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

import aiohttp
from aiohttp import web
import websockets

LOG = logging.getLogger("hermes-ableton.bridge")

DEFAULT_CONFIG = {
    "ws_host": "0.0.0.0",
    "ws_port": 8080,
    "http_host": "0.0.0.0",
    "http_port": 8081,
    "auth_token": "change-me-please",
    "ssl": {"enabled": False, "cert": "", "key": ""},
    "state_interval": 2.0,
    "command_timeout": 10.0,
    "log_level": "INFO",
}


# --------------------------------------------------------------------------- #
#  Bridge state
# --------------------------------------------------------------------------- #
class BridgeState:
    """Holds the single Ableton WS connection, last known state, and pending
    command futures keyed by message id."""

    def __init__(self, token: str, command_timeout: float):
        self.token = token
        self.command_timeout = command_timeout
        self.ableton_ws: Optional[websockets.WebSocketServerProtocol] = None
        self.last_state: Dict[str, Any] = {}
        self.pending: Dict[str, "asyncio.Future[Any]"] = {}
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        if self.ableton_ws is None:
            return False
        # websockets >=14 uses connection.state; legacy used .open
        try:
            return self.ableton_ws.state == websockets.protocol.State.OPEN  # type: ignore[attr-defined]
        except AttributeError:
            return bool(getattr(self.ableton_ws, "open", False))

    async def set_ableton(self, ws):
        async with self.lock:
            if self.ableton_ws is not None and self.connected and ws is not self.ableton_ws:
                LOG.warning("New Ableton connection replacing existing one.")
                try:
                    await self.ableton_ws.close(code=4001, reason="replaced")
                except Exception:
                    pass
            self.ableton_ws = ws
        LOG.info("Ableton client connected: %s", ws.remote_address)

    async def clear_ableton(self, ws):
        async with self.lock:
            if self.ableton_ws is ws:
                self.ableton_ws = None
                # fail any in-flight commands
                for mid, fut in list(self.pending.items()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("Ableton disconnected"))
                self.pending.clear()
        LOG.info("Ableton client disconnected: %s", ws.remote_address)

    async def send_command(self, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a command to Ableton and await its response."""
        if not self.connected or self.ableton_ws is None:
            raise ConnectionError("Ableton is not connected")
        msg_id = str(uuid.uuid4())
        params = params or {}
        payload = {
            "id": msg_id,
            "type": "command",
            "action": action,
            "params": params,
            "timestamp": int(time.time()),
        }
        fut: "asyncio.Future[Any]" = asyncio.get_event_loop().create_future()
        self.pending[msg_id] = fut
        try:
            await self.ableton_ws.send(json.dumps(payload))
            LOG.debug("-> ableton: %s %s", action, params)
            result = await asyncio.wait_for(fut, timeout=self.command_timeout)
            return result
        except asyncio.TimeoutError:
            self.pending.pop(msg_id, None)
            raise TimeoutError(f"Command '{action}' timed out after {self.command_timeout}s")
        except Exception:
            self.pending.pop(msg_id, None)
            raise

    async def resolve_response(self, msg: Dict[str, Any]):
        mid = msg.get("id")
        if not mid or mid not in self.pending:
            return
        fut = self.pending.pop(mid)
        if not fut.done():
            if msg.get("status") == "ok":
                fut.set_result(msg)
            else:
                fut.set_exception(
                    RuntimeError(msg.get("error") or "Ableton returned an error")
                )

    def update_state(self, data: Dict[str, Any]):
        self.last_state = data
        self.last_state["_updated_at"] = int(time.time())


# --------------------------------------------------------------------------- #
#  WebSocket endpoint (Ableton side)
# --------------------------------------------------------------------------- #
async def ableton_ws_handler(ws, state: BridgeState):
    # 1. Auth — first message must be {"auth": "TOKEN"}
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    except asyncio.TimeoutError:
        await ws.close(code=4003, reason="auth timeout")
        return
    try:
        auth_msg = json.loads(raw)
    except json.JSONDecodeError:
        await ws.close(code=4003, reason="bad auth json")
        return
    if auth_msg.get("auth") != state.token:
        LOG.warning("Ableton auth failed from %s", ws.remote_address)
        await ws.send(json.dumps({"type": "auth", "status": "error", "error": "bad token"}))
        await ws.close(code=4003, reason="bad token")
        return
    await ws.send(json.dumps({"type": "auth", "status": "ok"}))
    await state.set_ableton(ws)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                LOG.warning("Non-JSON message from Ableton: %r", raw[:200])
                continue
            mtype = msg.get("type")
            if mtype == "response":
                await state.resolve_response(msg)
            elif mtype == "state":
                state.update_state(msg.get("data", {}))
                LOG.debug("state updated: tempo=%s playing=%s",
                           msg.get("data", {}).get("tempo"),
                           msg.get("data", {}).get("playing"))
            elif mtype == "log":
                LOG.info("[Ableton] %s", msg.get("message", ""))
            else:
                LOG.debug("Unknown msg type from Ableton: %s", mtype)
    except websockets.ConnectionClosed:
        pass
    finally:
        await state.clear_ableton(ws)


# --------------------------------------------------------------------------- #
#  HTTP API (Hermes agent side)
# --------------------------------------------------------------------------- #
async def http_command(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    action = body.get("action")
    if not action:
        # allow raw {action, params} OR a wrapped command envelope
        return web.json_response({"error": "missing 'action'"}, status=400)
    params = body.get("params", {})
    if not isinstance(params, dict):
        return web.json_response({"error": "'params' must be an object"}, status=400)
    try:
        result = await state.send_command(action, params)
    except ConnectionError as e:
        return web.json_response({"status": "error", "error": str(e)}, status=503)
    except TimeoutError as e:
        return web.json_response({"status": "error", "error": str(e)}, status=504)
    except Exception as e:  # noqa: BLE001
        LOG.exception("command failed")
        return web.json_response({"status": "error", "error": str(e)}, status=500)
    return web.json_response(result)


async def http_state(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    return web.json_response({
        "connected": state.connected,
        "state": state.last_state,
    })


async def http_raw(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    if not state.connected:
        return web.json_response({"status": "error", "error": "Ableton not connected"}, status=503)
    try:
        msg = await request.text()
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=400)
    try:
        await state.ableton_ws.send(msg)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001
        return web.json_response({"status": "error", "error": str(e)}, status=500)
    return web.json_response({"status": "ok"})


async def http_status(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    return web.json_response({
        "status": "ok",
        "ableton_connected": state.connected,
        "ws_host": request.app["config"]["ws_host"],
        "ws_port": request.app["config"]["ws_port"],
    })


def build_http_app(state: BridgeState, config: Dict[str, Any]) -> web.Application:
    app = web.Application()
    app["state"] = state
    app["config"] = config
    app.router.add_post("/command", http_command)
    app.router.add_get("/state", http_state)
    app.router.add_post("/raw", http_raw)
    app.router.add_get("/status", http_status)
    return app


# --------------------------------------------------------------------------- #
#  Config loading
# --------------------------------------------------------------------------- #
def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path and Path(path).exists():
        if yaml is None:
            raise RuntimeError("PyYAML not installed; cannot read YAML config")
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        cfg.update(user)
    # env overrides
    import os
    if os.environ.get("BRIDGE_AUTH_TOKEN"):
        cfg["auth_token"] = os.environ["BRIDGE_AUTH_TOKEN"]
    if os.environ.get("BRIDGE_WS_PORT"):
        cfg["ws_port"] = int(os.environ["BRIDGE_WS_PORT"])
    if os.environ.get("BRIDGE_HTTP_PORT"):
        cfg["http_port"] = int(os.environ["BRIDGE_HTTP_PORT"])
    return cfg


def make_ssl_context(cfg: Dict[str, Any]) -> Optional[ssl.SSLContext]:
    sslcfg = cfg.get("ssl", {})
    if not sslcfg.get("enabled"):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(sslcfg["cert"], sslcfg["key"])
    return ctx


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
async def start_servers(cfg: Dict[str, Any]):
    """Start the WebSocket + HTTP servers and return a control dict.

    Returned dict keys: ws_server, runner, state, config, stop().
    Call ``await control["stop"]()`` (or stop_servers) to shut down.
    """
    state = BridgeState(token=cfg["auth_token"], command_timeout=cfg["command_timeout"])

    ssl_ctx = make_ssl_context(cfg)
    ws_srv = await websockets.serve(
        lambda ws: ableton_ws_handler(ws, state),
        cfg["ws_host"],
        cfg["ws_port"],
        ssl=ssl_ctx,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
    )
    LOG.info("WebSocket server listening on %s:%d (ssl=%s)",
             cfg["ws_host"], cfg["ws_port"], bool(ssl_ctx))

    app = build_http_app(state, cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg["http_host"], cfg["http_port"])
    await site.start()
    LOG.info("HTTP API listening on %s:%d", cfg["http_host"], cfg["http_port"])

    async def _stop():
        ws_srv.close()
        await ws_srv.wait_closed()
        await runner.cleanup()

    return {"ws_server": ws_srv, "runner": runner, "state": state,
            "config": cfg, "stop": _stop}


async def stop_servers(control: Dict[str, Any]):
    """Stop servers started by start_servers()."""
    await control["stop"]()


async def main_async(cfg: Dict[str, Any]):
    control = await start_servers(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    LOG.info("Shutting down...")
    await stop_servers(control)


def main():
    parser = argparse.ArgumentParser(description="Hermes-Ableton Bridge WebSocket server")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(main_async(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
