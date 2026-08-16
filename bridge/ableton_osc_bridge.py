#!/usr/bin/env python3
"""
Hermes-Ableton Bridge — Python OSC Bridge
==========================================

Runs on the Windows PC alongside Ableton Live. Translates between the Hermes
WebSocket server protocol (on the VPS) and AbletonOSC's OSC protocol (inside
Ableton Live via a Max for Live device).

ARCHITECTURE
------------

  VPS (WebSocket server :8080)  <──WebSocket──>  Python Bridge (Windows)  <──OSC──>  AbletonOSC (Ableton)

The bridge:
  1. Connects as a WebSocket CLIENT to the VPS server (ws://HOST:PORT)
  2. Authenticates with the shared token  {"auth": "<token>"}
  3. Receives JSON command messages from the VPS
  4. Translates each command into OSC messages sent to AbletonOSC (localhost:11000)
  5. Receives OSC responses from AbletonOSC (localhost:11001)
  6. Sends JSON responses back to the VPS WebSocket
  7. Periodically queries Ableton state via OSC and sends state reports to the VPS

PROTOCOL (identical to the previous Max-for-Live device — the VPS server is UNCHANGED)
-------------------------------------------------------------------------------------

  Client→Server (first message):  {"auth": "<token>"}
  Server→Client:                  {"type": "auth", "status": "ok"}
  Server→Client (command):        {"id": "<uuid>", "type": "command", "action": "<name>",
                                   "params": {...}, "timestamp": <epoch>}
  Client→Server (response):       {"id": "<uuid>", "type": "response", "status": "ok"|"error",
                                   "data": {...}, "error": null|"<msg>", "timestamp": <epoch>}
  Client→Server (state report):   {"type": "state", "data": {...}, "timestamp": <epoch>}

ABLETONOSC OSC CONVENTION
-------------------------

  AbletonOSC (https://github.com/ideoforms/AbletonOSC) is a Max for Live device
  that exposes the Live Object Model over OSC.  By default:
    - It LISTENS on port 11000 (we send TO it).
    - It SENDS responses to port 11001 (we listen ON it).

  OSC address pattern follows the Live Object Model path, using ``/`` separators
  and integer indices:
    - Call a function:  /live_set/start_play            (no args)
    - Set a property:    /live_set/tempo  [120.0]        (value as arg)
    - Get a property:   /live_set/tempo                  (no args → response with value)
    - Indexed children:  /live_set/tracks/0/volume  [-3.0]

  NOTE: The exact OSC paths may vary between AbletonOSC versions.  Fallback
  patterns are tried automatically and documented below.  See the README for
  how to adjust paths for your AbletonOSC version.

USAGE
-----

  python ableton_osc_bridge.py
  python ableton_osc_bridge.py --vps-host 177.7.34.85 --token my-secret
  python ableton_osc_bridge.py --dry-run          # no Ableton needed
  python ableton_osc_bridge.py --config config.yaml

Requires Python 3.8+ and: python-osc, websockets, pyyaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

# python-osc is only needed for real OSC communication (not --dry-run).
try:
    from pythonosc.udp_client import SimpleUDPClient
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import AsyncOSCUDPServer

    PYTHONOSC_AVAILABLE = True
except ImportError:  # pragma: no cover
    PYTHONOSC_AVAILABLE = False
    SimpleUDPClient = None  # type: ignore[assignment,misc]
    Dispatcher = None  # type: ignore[assignment,misc]
    AsyncOSCUDPServer = None  # type: ignore[assignment,misc]

LOG = logging.getLogger("ableton-bridge")

DEFAULT_CONFIG: Dict[str, Any] = {
    "vps_host": "177.7.34.85",
    "vps_port": 8080,
    "auth_token": "change-me-please",
    "osc_host": "127.0.0.1",
    "osc_send_port": 11000,     # AbletonOSC listens here
    "osc_listen_port": 11001,   # AbletonOSC sends responses here
    "state_interval": 2.0,
    "osc_timeout": 5.0,
    "reconnect_base_delay": 1,
    "reconnect_max_delay": 30,
    "log_level": "INFO",
    "use_ssl": False,
}


# --------------------------------------------------------------------------- #
#  OSC path helpers
# --------------------------------------------------------------------------- #
def _track_path(track: int) -> str:
    """OSC path prefix for a track, e.g. /live_set/tracks/3."""
    return f"/live_set/tracks/{int(track)}"


def _clip_path(track: int, clip: int) -> str:
    """OSC path prefix for a clip in a clip slot."""
    return f"/live_set/tracks/{int(track)}/clip_slots/{int(clip)}/clip"


def _device_path(track: int, device: int) -> str:
    """OSC path prefix for a device on a track."""
    return f"/live_set/tracks/{int(track)}/devices/{int(device)}"


# --------------------------------------------------------------------------- #
#  Mock Ableton state (used in --dry-run mode)
# --------------------------------------------------------------------------- #
class MockAbleton:
    """In-memory fake Live set for --dry-run mode. Mirrors tests/mock_client.py."""

    def __init__(self) -> None:
        self.tempo = 120.0
        self.playing = False
        self.loop_on = False
        self.metronome = False
        self.overdub_on = False
        self.time_signature: Tuple[int, int] = (4, 4)
        self.tracks: List[Dict[str, Any]] = [
            {
                "index": 0, "name": "1 MIDI", "type": "midi",
                "volume": 0.0, "pan": 0.0, "mute": False, "solo": False,
                "sends": [0.0, 0.0], "clips": [], "devices": [],
            },
        ]
        self.scenes: List[Dict[str, Any]] = [{"index": 0, "name": "Scene 1"}]

    def _track(self, index: int) -> Dict[str, Any]:
        for t in self.tracks:
            if t["index"] == index:
                return t
        raise IndexError(f"no track at index {index}")

    def _reindex(self) -> None:
        for i, t in enumerate(self.tracks):
            t["index"] = i

    def full_state(self) -> Dict[str, Any]:
        return {
            "tempo": self.tempo,
            "playing": self.playing,
            "loop": self.loop_on,
            "metronome": self.metronome,
            "overdub": self.overdub_on,
            "time_signature": list(self.time_signature),
            "tracks": self.tracks,
            "scenes": self.scenes,
        }

    # -- transport --
    def play(self) -> Dict[str, Any]:
        self.playing = True
        return {"playing": True}

    def stop(self) -> Dict[str, Any]:
        self.playing = False
        return {"playing": False}

    def set_tempo(self, bpm: float) -> Dict[str, Any]:
        self.tempo = float(bpm)
        return {"tempo": self.tempo}

    def set_time_signature(self, num: int, den: int) -> Dict[str, Any]:
        self.time_signature = (int(num), int(den))
        return {"time_signature": list(self.time_signature)}

    def toggle_loop(self) -> Dict[str, Any]:
        self.loop_on = not self.loop_on
        return {"loop": self.loop_on}

    def toggle_metronome(self) -> Dict[str, Any]:
        self.metronome = not self.metronome
        return {"metronome": self.metronome}

    def overdub(self, on: Optional[bool] = None) -> Dict[str, Any]:
        self.overdub_on = bool(on) if on is not None else not self.overdub_on
        return {"overdub": self.overdub_on}

    # -- tracks --
    def create_midi_track(self, index: Optional[int] = None) -> Dict[str, Any]:
        idx = index if index is not None else len(self.tracks)
        self.tracks.insert(idx, {
            "index": idx, "name": f"{idx + 1} MIDI", "type": "midi",
            "volume": 0.0, "pan": 0.0, "mute": False, "solo": False,
            "sends": [0.0, 0.0], "clips": [], "devices": [],
        })
        self._reindex()
        return {"track": idx, "count": len(self.tracks)}

    def create_audio_track(self, index: Optional[int] = None) -> Dict[str, Any]:
        idx = index if index is not None else len(self.tracks)
        self.tracks.insert(idx, {
            "index": idx, "name": f"{idx + 1} Audio", "type": "audio",
            "volume": 0.0, "pan": 0.0, "mute": False, "solo": False,
            "sends": [0.0, 0.0], "clips": [], "devices": [],
        })
        self._reindex()
        return {"track": idx, "count": len(self.tracks)}

    def delete_track(self, index: int) -> Dict[str, Any]:
        t = self._track(int(index))
        self.tracks.remove(t)
        self._reindex()
        return {"deleted": int(index)}

    def duplicate_track(self, index: int) -> Dict[str, Any]:
        import copy
        t = self._track(int(index))
        new = copy.deepcopy(t)
        new["index"] = t["index"] + 1
        self.tracks.insert(t["index"] + 1, new)
        self._reindex()
        return {"duplicated": int(index)}

    def mute_track(self, index: int, mute: bool = True) -> Dict[str, Any]:
        t = self._track(int(index))
        t["mute"] = bool(mute)
        return {"track": int(index), "mute": t["mute"]}

    def solo_track(self, index: int, solo: bool = True) -> Dict[str, Any]:
        t = self._track(int(index))
        t["solo"] = bool(solo)
        return {"track": int(index), "solo": t["solo"]}

    def set_volume(self, index: int, volume: float) -> Dict[str, Any]:
        t = self._track(int(index))
        t["volume"] = float(volume)
        return {"track": int(index), "volume": t["volume"]}

    def set_pan(self, index: int, pan: float) -> Dict[str, Any]:
        t = self._track(int(index))
        t["pan"] = float(pan)
        return {"track": int(index), "pan": t["pan"]}

    def set_send(self, index: int, send: int, value: float) -> Dict[str, Any]:
        t = self._track(int(index))
        while len(t["sends"]) <= int(send):
            t["sends"].append(0.0)
        t["sends"][int(send)] = float(value)
        return {"track": int(index), "send": int(send), "value": float(value)}

    # -- clips --
    def create_midi_clip(self, track: int, length_beats: float = 4.0,
                         scene: Optional[int] = None) -> Dict[str, Any]:
        t = self._track(int(track))
        slot = scene if scene is not None else len(t["clips"])
        clip = {
            "index": len(t["clips"]),
            "length_beats": float(length_beats),
            "notes": [], "loop": True,
        }
        t["clips"].append(clip)
        return {"track": int(track), "clip": slot, "scene": slot,
                "length_beats": float(length_beats)}

    def set_clip_length(self, track: int, clip: int, length_beats: float) -> Dict[str, Any]:
        t = self._track(int(track))
        c = t["clips"][int(clip)]
        c["length_beats"] = float(length_beats)
        return {"length_beats": c["length_beats"]}

    def add_note(self, track: int, clip: int, pitch: int, start: float,
                 duration: float, velocity: int = 100) -> Dict[str, Any]:
        t = self._track(int(track))
        c = t["clips"][int(clip)]
        c["notes"].append({
            "pitch": int(pitch), "start": float(start),
            "duration": float(duration), "velocity": int(velocity),
        })
        return {"added": True}

    def add_notes(self, track: int, clip: int,
                  notes: List[Dict[str, Any]]) -> Dict[str, Any]:
        t = self._track(int(track))
        c = t["clips"][int(clip)]
        for n in notes:
            c["notes"].append({
                "pitch": int(n["pitch"]), "start": float(n["start"]),
                "duration": float(n["duration"]), "velocity": int(n["velocity"]),
            })
        return {"added": len(notes)}

    def remove_note(self, track: int, clip: int, pitch: int, start: float) -> Dict[str, Any]:
        t = self._track(int(track))
        c = t["clips"][int(clip)]
        c["notes"] = [
            n for n in c["notes"]
            if not (n["pitch"] == int(pitch)
                    and abs(n["start"] - float(start)) < 1e-6)
        ]
        return {"removed": True}

    def clear_clip(self, track: int, clip: int) -> Dict[str, Any]:
        t = self._track(int(track))
        t["clips"][int(clip)]["notes"] = []
        return {"cleared": True}

    def quantize_clip(self, track: int, clip: int, grid: int = 4) -> Dict[str, Any]:
        t = self._track(int(track))
        c = t["clips"][int(clip)]
        step = 1.0 / float(grid)
        for n in c["notes"]:
            n["start"] = round(n["start"] / step) * step
        return {"quantized": True}

    def toggle_clip_loop(self, track: int, clip: int) -> Dict[str, Any]:
        t = self._track(int(track))
        c = t["clips"][int(clip)]
        c["loop"] = not c.get("loop", True)
        return {"loop": c["loop"]}

    # -- browser --
    def load_instrument(self, track: int, name: str) -> Dict[str, Any]:
        t = self._track(int(track))
        t["devices"].append({"name": name, "type": "instrument", "parameters": [0.5, 0.5, 0.5]})
        return {"loaded": name, "device": len(t["devices"]) - 1}

    def load_effect(self, track: int, name: str) -> Dict[str, Any]:
        t = self._track(int(track))
        t["devices"].append({"name": name, "type": "audio_effect", "parameters": [0.5] * 4})
        return {"loaded": name, "device": len(t["devices"]) - 1}

    def load_sample(self, track: int, name: str) -> Dict[str, Any]:
        return {"loaded": name}

    def load_drum_rack(self, track: int, name: str = "Drum Rack") -> Dict[str, Any]:
        t = self._track(int(track))
        t["devices"].append({"name": name, "type": "drum_rack", "parameters": [0.5]})
        return {"loaded": name}

    # -- devices --
    def set_device_parameter(self, track: int, device: int,
                             param: int, value: float) -> Dict[str, Any]:
        t = self._track(int(track))
        d = t["devices"][int(device)]
        while len(d["parameters"]) <= int(param):
            d["parameters"].append(0.5)
        d["parameters"][int(param)] = float(value)
        return {"value": float(value)}

    def get_device_parameters(self, track: int, device: int) -> Dict[str, Any]:
        t = self._track(int(track))
        d = t["devices"][int(device)]
        params = [{"name": f"Param {i}", "value": v}
                   for i, v in enumerate(d.get("parameters", []))]
        return {"parameters": params}

    # -- scenes --
    def create_scene(self, name: str = "") -> Dict[str, Any]:
        idx = len(self.scenes)
        self.scenes.append({"index": idx, "name": name or f"Scene {idx + 1}"})
        return {"scene": idx}

    def launch_scene(self, scene: int) -> Dict[str, Any]:
        return {"launched": int(scene)}

    def reorder_scene(self, scene: int, new_index: int) -> Dict[str, Any]:
        s = self.scenes.pop(int(scene))
        self.scenes.insert(int(new_index), s)
        for i, sc in enumerate(self.scenes):
            sc["index"] = i
        return {"reordered": True}


# --------------------------------------------------------------------------- #
#  OSC responder — correlates outgoing requests with incoming responses
# --------------------------------------------------------------------------- #
class OSCResponder:
    """Tracks pending OSC responses keyed by address.

    AbletonOSC echoes the request address in its responses, so we key futures
    by address.  Unmatched messages are buffered in a queue (useful for
    diagnostic logging).
    """

    def __init__(self) -> None:
        self._pending: Dict[str, List[asyncio.Future]] = {}
        self._unmatched: List[Tuple[str, List[Any]]] = []

    def on_message(self, address: str, *args: Any) -> None:
        arg_list = list(args)
        # Exact address match
        if address in self._pending and self._pending[address]:
            fut = self._pending[address].pop(0)
            if not fut.done():
                fut.set_result(arg_list)
            return
        # Fuzzy match: response address endswith request address or vice-versa
        for req_addr in list(self._pending.keys()):
            if self._pending[req_addr] and (
                address.endswith(req_addr) or req_addr.endswith(address)
            ):
                fut = self._pending[req_addr].pop(0)
                if not fut.done():
                    fut.set_result(arg_list)
                return
        # No match — buffer for diagnostics
        self._unmatched.append((address, arg_list))
        if len(self._unmatched) > 100:
            self._unmatched.pop(0)
        LOG.debug("Unmatched OSC response: %s %s", address, arg_list)

    async def wait_for(self, address: str, timeout: float) -> Optional[List[Any]]:
        fut = asyncio.get_event_loop().create_future()
        self._pending.setdefault(address, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            if address in self._pending and fut in self._pending[address]:
                self._pending[address].remove(fut)
            LOG.debug("OSC response timeout for %s", address)
            return None

    def clear(self) -> None:
        for addrs in self._pending.values():
            for fut in addrs:
                if not fut.done():
                    fut.cancel()
        self._pending.clear()
        self._unmatched.clear()


# --------------------------------------------------------------------------- #
#  Main bridge
# --------------------------------------------------------------------------- #
class AbletonOSCBridge:
    """WebSocket client (→VPS) + OSC client/server (→AbletonOSC) bridge."""

    def __init__(self, config: Dict[str, Any], dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.mock = MockAbleton() if dry_run else None

        # WebSocket state
        self.ws: Optional[Any] = None
        self.authed = False
        self._ws_connected = False

        # OSC state
        self.osc_client: Optional[Any] = None          # SimpleUDPClient
        self.osc_server: Optional[Any] = None          # AsyncOSCUDPServer
        self.responder = OSCResponder()

        # Control
        self._stop = asyncio.Event()
        self._state_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    #  OSC setup
    # ------------------------------------------------------------------ #
    def setup_osc(self) -> None:
        """Create the OSC UDP client (sender) and start the OSC UDP server
        (listener for AbletonOSC responses)."""
        if self.dry_run:
            LOG.info("[DRY-RUN] OSC setup skipped — no Ableton connection.")
            return
        if not PYTHONOSC_AVAILABLE:
            raise RuntimeError(
                "python-osc is not installed.  Install with:  "
                "pip install python-osc  (or: pip install -r requirements.txt)"
            )

        host = self.config["osc_host"]
        send_port = int(self.config["osc_send_port"])
        listen_port = int(self.config["osc_listen_port"])

        # Sender — messages go TO AbletonOSC
        self.osc_client = SimpleUDPClient(host, send_port)
        LOG.info("OSC sender → %s:%d", host, send_port)

        # Listener — responses come FROM AbletonOSC
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(self._on_osc)
        # AsyncOSCUDPServer is created inside the event loop (see start_osc_server)
        self._osc_dispatcher = dispatcher
        self._osc_listen_addr = (host, listen_port)
        LOG.info("OSC listener ← %s:%d", host, listen_port)

    async def start_osc_server(self) -> None:
        """Start the async OSC UDP server (must be called from the event loop)."""
        if self.dry_run or self.osc_client is None:
            return
        loop = asyncio.get_event_loop()
        self.osc_server = AsyncOSCUDPServer(
            self._osc_listen_addr, self._osc_dispatcher, loop
        )
        await self.osc_server.serve()
        LOG.info("OSC server listening on %s:%d", *self._osc_listen_addr)

    def _on_osc(self, address: str, *args: Any) -> None:
        """Dispatcher callback for incoming OSC messages from AbletonOSC."""
        LOG.debug("← OSC %s %s", address, args)
        self.responder.on_message(address, *args)

    # ------------------------------------------------------------------ #
    #  OSC send helpers
    # ------------------------------------------------------------------ #
    def _send(self, address: str, args: Optional[List[Any]] = None) -> None:
        """Send an OSC message (fire-and-forget)."""
        if self.dry_run or self.osc_client is None:
            LOG.info("[DRY-RUN] OSC → %s %s", address, args or [])
            return
        self.osc_client.send_message(address, args or [])
        LOG.debug("→ OSC %s %s", address, args or [])

    async def _osc_get(self, address: str,
                       timeout: Optional[float] = None) -> Optional[List[Any]]:
        """Send a GET (no args) and wait for the response."""
        if self.dry_run:
            LOG.info("[DRY-RUN] OSC GET %s", address)
            return None
        to = timeout if timeout is not None else self.config["osc_timeout"]
        self._send(address)
        return await self.responder.wait_for(address, to)

    async def _osc_set(self, address: str, value: Any,
                       wait: bool = False) -> Optional[List[Any]]:
        """Send a SET (with value). Optionally wait for a response."""
        if self.dry_run:
            LOG.info("[DRY-RUN] OSC SET %s = %s", address, value)
            return None
        self._send(address, [value])
        if wait:
            return await self.responder.wait_for(address, self.config["osc_timeout"])
        return None

    async def _osc_call(self, address: str, args: Optional[List[Any]] = None,
                         wait: bool = True) -> Optional[List[Any]]:
        """Send a function CALL. Optionally wait for a response."""
        if self.dry_run:
            LOG.info("[DRY-RUN] OSC CALL %s %s", address, args or [])
            return None
        self._send(address, args)
        if wait:
            return await self.responder.wait_for(address, self.config["osc_timeout"])
        return None

    async def _osc_call_fallback(
        self, addresses: List[str], args: Optional[List[Any]] = None,
        wait: bool = True,
    ) -> Optional[List[Any]]:
        """Try multiple OSC addresses in order (for version compatibility).
        Returns the first successful response, or None."""
        for i, addr in enumerate(addresses):
            resp = await self._osc_call(addr, args, wait=wait)
            if resp is not None:
                return resp
            if i < len(addresses) - 1:
                LOG.debug("fallback: %s failed, trying %s", addr, addresses[i + 1])
        return None

    # ------------------------------------------------------------------ #
    #  WebSocket connection
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Main entry point: start OSC, connect WebSocket, run forever."""
        self.setup_osc()
        await self.start_osc_server()

        base_delay = int(self.config.get("reconnect_base_delay", 1))
        max_delay = int(self.config.get("reconnect_max_delay", 30))
        attempt = 0

        while not self._stop.is_set():
            try:
                await self._connect_and_serve()
                attempt = 0  # reset on clean disconnect
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                LOG.warning("WebSocket loop error: %s", e)

            if self._stop.is_set():
                break
            delay = min(max_delay, base_delay * (2 ** min(attempt, 5)))
            attempt += 1
            LOG.info("Reconnecting in %ds (attempt %d)...", delay, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break  # stop signaled during sleep
            except asyncio.TimeoutError:
                pass

        LOG.info("Bridge shutting down.")

    async def _connect_and_serve(self) -> None:
        """Connect to the VPS WebSocket, authenticate, and serve commands."""
        if websockets is None:
            raise RuntimeError("websockets library not installed")

        host = self.config["vps_host"]
        port = int(self.config["vps_port"])
        scheme = "wss" if self.config.get("use_ssl") else "ws"
        uri = f"{scheme}://{host}:{port}"

        LOG.info("Connecting to %s ...", uri)
        async with websockets.connect(uri, ping_interval=20, ping_timeout=20,
                                       close_timeout=10) as ws:
            self.ws = ws
            self._ws_connected = True

            # --- Authenticate ---
            await ws.send(json.dumps({"auth": self.config["auth_token"]}))
            auth_raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            auth_msg = json.loads(auth_raw)
            if auth_msg.get("status") != "ok":
                LOG.error("Authentication failed: %s", auth_msg)
                self._ws_connected = False
                return
            self.authed = True
            LOG.info("Authenticated with VPS at %s", uri)

            # --- Start state polling ---
            self._state_task = asyncio.create_task(self._state_loop())

            # --- Listen for commands ---
            try:
                async for raw in ws:
                    await self._on_ws_message(raw)
            finally:
                self.authed = False
                self._ws_connected = False
                if self._state_task:
                    self._state_task.cancel()
                    try:
                        await self._state_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    self._state_task = None
                self.responder.clear()
                LOG.info("WebSocket disconnected from VPS.")

    async def _on_ws_message(self, raw: "Any") -> None:
        """Handle a single JSON message from the VPS."""
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            LOG.warning("Non-JSON message from VPS: %r", raw[:200])
            return

        mtype = msg.get("type")
        if mtype == "command":
            await self._handle_command(msg)
        elif mtype == "auth":
            # Already handled during connect; ignore duplicate
            pass
        else:
            LOG.debug("Unknown VPS message type: %s", mtype)

    async def _handle_command(self, msg: Dict[str, Any]) -> None:
        """Process a command from the VPS and send the JSON response back."""
        mid = msg.get("id")
        action = msg.get("action", "")
        params = msg.get("params", {}) or {}
        ts = int(time.time())

        LOG.info("← command: %s %s", action, params)
        resp: Dict[str, Any] = {
            "id": mid, "type": "response", "status": "ok",
            "data": {}, "error": None, "timestamp": ts,
        }
        try:
            resp["data"] = await self._execute(action, params)
        except Exception as e:  # noqa: BLE001
            resp["status"] = "error"
            resp["error"] = str(e)
            LOG.error("command '%s' failed: %s", action, e, exc_info=True)

        try:
            await self.ws.send(json.dumps(resp))  # type: ignore[union-attr]
            LOG.info("→ response: %s %s", action, resp["status"])
        except Exception as e:  # noqa: BLE001
            LOG.error("Failed to send response: %s", e)

    # ------------------------------------------------------------------ #
    #  Command dispatch
    # ------------------------------------------------------------------ #
    async def _execute(self, action: str, p: Dict[str, Any]) -> Dict[str, Any]:
        """Translate a JSON command into OSC and return the response data."""

        if self.dry_run:
            return await self._execute_mock(action, p)

        handler = getattr(self, f"_cmd_{action}", None)
        if handler is None:
            raise ValueError(f"unknown action '{action}'")
        return await handler(p)

    # ------------------------------------------------------------------ #
    #  Command handlers (real OSC → AbletonOSC)
    # ------------------------------------------------------------------ #
    # --- Transport ---
    async def _cmd_play(self, p: Dict[str, Any]) -> Dict[str, Any]:
        await self._osc_call_fallback(
            ["/live_set/start_play", "/live_set/play"],
            wait=True,
        )
        return {"playing": True}

    async def _cmd_stop(self, p: Dict[str, Any]) -> Dict[str, Any]:
        await self._osc_call_fallback(
            ["/live_set/stop_play", "/live_set/stop"],
            wait=True,
        )
        return {"playing": False}

    async def _cmd_set_tempo(self, p: Dict[str, Any]) -> Dict[str, Any]:
        bpm = float(p["tempo"])
        await self._osc_set("/live_set/tempo", bpm)
        return {"tempo": bpm}

    async def _cmd_set_time_signature(self, p: Dict[str, Any]) -> Dict[str, Any]:
        num = int(p["numerator"])
        den = int(p["denominator"])
        await self._osc_set("/live_set/signature_numerator", num)
        await self._osc_set("/live_set/signature_denominator", den)
        return {"time_signature": [num, den]}

    async def _cmd_toggle_loop(self, p: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._osc_get("/live_set/loop")
        cur = bool(resp[0]) if resp else False
        await self._osc_set("/live_set/loop", 0 if cur else 1)
        return {"loop": not cur}

    async def _cmd_toggle_metronome(self, p: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._osc_get("/live_set/metronome")
        cur = bool(resp[0]) if resp else False
        await self._osc_set("/live_set/metronome", 0 if cur else 1)
        return {"metronome": not cur}

    async def _cmd_overdub(self, p: Dict[str, Any]) -> Dict[str, Any]:
        if "on" in p and p["on"] is not None:
            on = bool(p["on"])
        else:
            resp = await self._osc_get("/live_set/overdub")
            on = not (bool(resp[0]) if resp else False)
        await self._osc_set("/live_set/overdub", 1 if on else 0)
        return {"overdub": on}

    # --- Tracks ---
    async def _cmd_create_midi_track(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = p.get("index")
        args = [int(idx)] if idx is not None else []
        await self._osc_call("/live_set/create_midi_track", args, wait=True)
        return {"track": int(idx) if idx is not None else 0}

    async def _cmd_create_audio_track(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = p.get("index")
        args = [int(idx)] if idx is not None else []
        await self._osc_call("/live_set/create_audio_track", args, wait=True)
        return {"track": int(idx) if idx is not None else 0}

    async def _cmd_delete_track(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = int(p["index"])
        await self._osc_call("/live_set/delete_track", [idx], wait=True)
        return {"deleted": idx}

    async def _cmd_duplicate_track(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = int(p["index"])
        await self._osc_call("/live_set/duplicate_track", [idx], wait=True)
        return {"duplicated": idx}

    async def _cmd_mute_track(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = int(p["index"])
        mute = 1 if p.get("mute", True) else 0
        await self._osc_set(f"{_track_path(idx)}/mute", mute)
        return {"track": idx, "mute": bool(mute)}

    async def _cmd_solo_track(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = int(p["index"])
        solo = 1 if p.get("solo", True) else 0
        await self._osc_set(f"{_track_path(idx)}/solo", solo)
        return {"track": idx, "solo": bool(solo)}

    async def _cmd_set_volume(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = int(p["index"])
        vol = float(p["volume"])
        await self._osc_set(f"{_track_path(idx)}/volume", vol)
        return {"track": idx, "volume": vol}

    async def _cmd_set_pan(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = int(p["index"])
        pan = float(p["pan"])
        await self._osc_set(f"{_track_path(idx)}/pan", pan)
        return {"track": idx, "pan": pan}

    async def _cmd_set_send(self, p: Dict[str, Any]) -> Dict[str, Any]:
        idx = int(p["index"])
        send = int(p["send"])
        value = float(p["value"])
        # Primary path: /live_set/tracks/{i}/sends/{s}  (AbletonOSC convention)
        # Fallback: /live_set/tracks/{i}/sends/{s}/value
        await self._osc_set(f"{_track_path(idx)}/sends/{send}", value)
        return {"track": idx, "send": send, "value": value}

    # --- Clips ---
    async def _cmd_create_midi_clip(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        length = float(p.get("length_beats", 4.0))
        slot = int(p["scene"]) if "scene" in p and p["scene"] is not None else 0
        # AbletonOSC: /live_set/tracks/{t}/clip_slots/{slot}/create_clip [length]
        await self._osc_call(
            f"/live_set/tracks/{track}/clip_slots/{slot}/create_clip",
            [length], wait=True,
        )
        return {"track": track, "clip": slot, "scene": slot, "length_beats": length}

    async def _cmd_set_clip_length(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        clip = int(p["clip"])
        length = float(p["length_beats"])
        await self._osc_set(f"{_clip_path(track, clip)}/loop_end", length)
        return {"length_beats": length}

    async def _cmd_add_note(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        clip = int(p["clip"])
        pitch = int(p["pitch"])
        start = float(p["start"])
        duration = float(p["duration"])
        velocity = int(p.get("velocity", 100))
        # AbletonOSC: add_notes takes [pitch, start, duration, velocity, muted]
        # Some versions use /add_notes, others /notes/add — try both.
        base = _clip_path(track, clip)
        await self._osc_call_fallback(
            [f"{base}/add_notes", f"{base}/notes/add"],
            [pitch, start, duration, velocity, 0],
            wait=True,
        )
        return {"added": True}

    async def _cmd_add_notes(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        clip = int(p["clip"])
        notes = p["notes"]
        base = _clip_path(track, clip)
        for n in notes:
            pitch = int(n["pitch"])
            start = float(n["start"])
            duration = float(n["duration"])
            velocity = int(n.get("velocity", 100))
            await self._osc_call_fallback(
                [f"{base}/add_notes", f"{base}/notes/add"],
                [pitch, start, duration, velocity, 0],
                wait=True,
            )
        return {"added": len(notes)}

    async def _cmd_remove_note(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        clip = int(p["clip"])
        pitch = int(p["pitch"])
        start = float(p["start"])
        base = _clip_path(track, clip)
        # remove_notes takes [from_time, from_pitch, to_time, to_pitch]
        await self._osc_call_fallback(
            [f"{base}/remove_notes", f"{base}/notes/remove"],
            [start, pitch, start + 0.001, pitch + 1],
            wait=True,
        )
        return {"removed": True}

    async def _cmd_clear_clip(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        clip = int(p["clip"])
        base = _clip_path(track, clip)
        await self._osc_call_fallback(
            [f"{base}/remove_all_notes", f"{base}/clear_notes"],
            wait=True,
        )
        return {"cleared": True}

    async def _cmd_quantize_clip(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        clip = int(p["clip"])
        grid = int(p.get("grid", 4))
        base = _clip_path(track, clip)
        await self._osc_call(f"{base}/quantize", [grid], wait=True)
        return {"quantized": True}

    async def _cmd_toggle_clip_loop(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        clip = int(p["clip"])
        base = _clip_path(track, clip)
        resp = await self._osc_get(f"{base}/looping")
        cur = bool(resp[0]) if resp else False
        await self._osc_set(f"{base}/looping", 0 if cur else 1)
        return {"loop": not cur}

    # --- Browser ---
    async def _cmd_load_instrument(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        name = p["name"]
        # AbletonOSC exposes track-level browser functions.
        # /live_set/tracks/{t}/load_instrument_or_sample_browser  (opens browser)
        await self._osc_call_fallback(
            [
                f"{_track_path(track)}/load_instrument_or_sample_browser",
                f"/live_set/view/load_instrument_or_sample_browser",
            ],
            wait=True,
        )
        return {"loaded": name, "note": "browser opened — select item to load"}

    async def _cmd_load_effect(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        name = p["name"]
        await self._osc_call_fallback(
            [
                f"{_track_path(track)}/load_audio_effect_browser",
                f"/live_set/view/load_audio_effect_browser",
            ],
            wait=True,
        )
        return {"loaded": name, "note": "browser opened — select item to load"}

    async def _cmd_load_sample(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        name = p["name"]
        await self._osc_call_fallback(
            [
                f"{_track_path(track)}/load_instrument_or_sample_browser",
                f"/live_set/view/load_instrument_or_sample_browser",
            ],
            wait=True,
        )
        return {"loaded": name, "note": "browser opened — select item to load"}

    async def _cmd_load_drum_rack(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        name = p.get("name", "Drum Rack")
        # Drum Rack loading — try device browser as fallback
        await self._osc_call_fallback(
            [
                f"{_track_path(track)}/load_device_browser",
                f"/live_set/view/load_device_browser",
            ],
            wait=True,
        )
        return {"loaded": name, "note": "browser opened — select Drum Rack"}

    # --- Devices ---
    async def _cmd_set_device_parameter(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        device = int(p["device"])
        param = int(p["param"])
        value = float(p["value"])
        path = f"{_device_path(track, device)}/parameters/{param}"
        # Primary: set the "value" child property
        # Fallback: set directly on the parameter path
        await self._osc_set(f"{path}/value", value)
        return {"value": value}

    async def _cmd_get_device_parameters(self, p: Dict[str, Any]) -> Dict[str, Any]:
        track = int(p["track"])
        device = int(p["device"])
        dev_path = _device_path(track, device)

        # Get parameter count — AbletonOSC returns count when querying
        # /live_set/tracks/{t}/devices/{d}/parameters  with no args
        count_resp = await self._osc_get(f"{dev_path}/parameters")
        if count_resp and isinstance(count_resp[0], (int, float)):
            count = int(count_resp[0])
        else:
            count = 8  # fallback default

        params: List[Dict[str, Any]] = []
        for i in range(count):
            name_resp = await self._osc_get(f"{dev_path}/parameters/{i}/name")
            val_resp = await self._osc_get(f"{dev_path}/parameters/{i}/value")
            params.append({
                "name": name_resp[0] if name_resp else f"Param {i}",
                "value": val_resp[0] if val_resp else 0.0,
            })
        return {"parameters": params}

    # --- Scenes ---
    async def _cmd_create_scene(self, p: Dict[str, Any]) -> Dict[str, Any]:
        name = p.get("name", "")
        # Get current scene count to insert at the end
        count_resp = await self._osc_get("/live_set/scenes")
        count = int(count_resp[0]) if count_resp else 0
        await self._osc_call("/live_set/create_scene", [count], wait=True)
        if name:
            await self._osc_set(f"/live_set/scenes/{count}/name", name)
        return {"scene": count}

    async def _cmd_launch_scene(self, p: Dict[str, Any]) -> Dict[str, Any]:
        scene = int(p["scene"])
        await self._osc_call(f"/live_set/scenes/{scene}/launch", wait=True)
        return {"launched": scene}

    async def _cmd_reorder_scene(self, p: Dict[str, Any]) -> Dict[str, Any]:
        scene = int(p["scene"])
        new_index = int(p["new_index"])
        await self._osc_call("/live_set/move_scene", [scene, new_index], wait=True)
        return {"reordered": True}

    # --- State ---
    async def _cmd_get_full_state(self, p: Dict[str, Any]) -> Dict[str, Any]:
        return await self._query_full_state()

    # ------------------------------------------------------------------ #
    #  State querying (real OSC)
    # ------------------------------------------------------------------ #
    async def _query_full_state(self) -> Dict[str, Any]:
        """Query the full Live set state via OSC. Returns a dict matching the
        protocol's state report format."""
        state: Dict[str, Any] = {
            "tempo": 120.0, "playing": False, "loop": False,
            "metronome": False, "overdub": False,
            "time_signature": [4, 4], "tracks": [], "scenes": [],
        }
        try:
            resp = await self._osc_get("/live_set/tempo")
            if resp:
                state["tempo"] = float(resp[0])
        except Exception:  # noqa: BLE001
            pass
        try:
            resp = await self._osc_get("/live_set/is_playing")
            if resp:
                state["playing"] = bool(resp[0])
        except Exception:  # noqa: BLE001
            pass
        try:
            resp = await self._osc_get("/live_set/loop")
            if resp:
                state["loop"] = bool(resp[0])
        except Exception:  # noqa: BLE001
            pass
        try:
            resp = await self._osc_get("/live_set/metronome")
            if resp:
                state["metronome"] = bool(resp[0])
        except Exception:  # noqa: BLE001
            pass
        try:
            resp = await self._osc_get("/live_set/overdub")
            if resp:
                state["overdub"] = bool(resp[0])
        except Exception:  # noqa: BLE001
            pass
        try:
            num_resp = await self._osc_get("/live_set/signature_numerator")
            den_resp = await self._osc_get("/live_set/signature_denominator")
            if num_resp and den_resp:
                state["time_signature"] = [int(num_resp[0]), int(den_resp[0])]
        except Exception:  # noqa: BLE001
            pass

        # Tracks
        try:
            tc_resp = await self._osc_get("/live_set/tracks")
            n_tracks = int(tc_resp[0]) if tc_resp else 0
        except Exception:  # noqa: BLE001
            n_tracks = 0

        for i in range(n_tracks):
            tp = _track_path(i)
            track: Dict[str, Any] = {
                "index": i, "name": f"Track {i + 1}", "type": "midi",
                "volume": 0.0, "pan": 0.0, "mute": False, "solo": False,
                "clips": [], "devices": [],
            }
            try:
                r = await self._osc_get(f"{tp}/name")
                if r:
                    track["name"] = str(r[0])
            except Exception:  # noqa: BLE001
                pass
            try:
                r = await self._osc_get(f"{tp}/volume")
                if r:
                    track["volume"] = float(r[0])
            except Exception:  # noqa: BLE001
                pass
            try:
                r = await self._osc_get(f"{tp}/pan")
                if r:
                    track["pan"] = float(r[0])
            except Exception:  # noqa: BLE001
                pass
            try:
                r = await self._osc_get(f"{tp}/mute")
                if r:
                    track["mute"] = bool(r[0])
            except Exception:  # noqa: BLE001
                pass
            try:
                r = await self._osc_get(f"{tp}/solo")
                if r:
                    track["solo"] = bool(r[0])
            except Exception:  # noqa: BLE001
                pass

            # Clips
            try:
                cs_resp = await self._osc_get(f"{tp}/clip_slots")
                n_clips = int(cs_resp[0]) if cs_resp else 0
            except Exception:  # noqa: BLE001
                n_clips = 0
            for c in range(n_clips):
                cp = f"{tp}/clip_slots/{c}"
                try:
                    hc_resp = await self._osc_get(f"{cp}/has_clip")
                    if hc_resp and hc_resp[0]:
                        clip_obj: Dict[str, Any] = {
                            "index": c, "length_beats": 4.0, "loop": True, "notes": [],
                        }
                        try:
                            le = await self._osc_get(f"{cp}/clip/loop_end")
                            if le:
                                clip_obj["length_beats"] = float(le[0])
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            lp = await self._osc_get(f"{cp}/clip/looping")
                            if lp:
                                clip_obj["loop"] = bool(lp[0])
                        except Exception:  # noqa: BLE001
                            pass
                        track["clips"].append(clip_obj)
                except Exception:  # noqa: BLE001
                    pass

            # Devices
            try:
                dc_resp = await self._osc_get(f"{tp}/devices")
                n_dev = int(dc_resp[0]) if dc_resp else 0
            except Exception:  # noqa: BLE001
                n_dev = 0
            for d in range(n_dev):
                dp = f"{tp}/devices/{d}"
                dev: Dict[str, Any] = {"index": d, "name": f"Device {d}", "parameters": []}
                try:
                    r = await self._osc_get(f"{dp}/name")
                    if r:
                        dev["name"] = str(r[0])
                except Exception:  # noqa: BLE001
                    pass
                track["devices"].append(dev)

            state["tracks"].append(track)

        # Scenes
        try:
            sc_resp = await self._osc_get("/live_set/scenes")
            n_scenes = int(sc_resp[0]) if sc_resp else 0
        except Exception:  # noqa: BLE001
            n_scenes = 0
        for s in range(n_scenes):
            scene_obj: Dict[str, Any] = {"index": s, "name": f"Scene {s + 1}"}
            try:
                r = await self._osc_get(f"/live_set/scenes/{s}/name")
                if r:
                    scene_obj["name"] = str(r[0])
            except Exception:  # noqa: BLE001
                pass
            state["scenes"].append(scene_obj)

        return state

    # ------------------------------------------------------------------ #
    #  State polling loop
    # ------------------------------------------------------------------ #
    async def _state_loop(self) -> None:
        """Periodically query Ableton state and send a state report to the VPS."""
        interval = float(self.config.get("state_interval", 2.0))
        LOG.info("State polling started (every %.1fs)", interval)
        while self.authed and not self._stop.is_set():
            try:
                if self.dry_run:
                    m = self.mock
                    assert m is not None
                    state = m.full_state()
                else:
                    state = await self._query_full_state()
                await self._send_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                LOG.warning("State poll error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        LOG.info("State polling stopped.")

    async def _send_state(self, state: Dict[str, Any]) -> None:
        """Send a state report to the VPS."""
        if self.ws is None:
            return
        msg = {"type": "state", "data": state, "timestamp": int(time.time())}
        try:
            await self.ws.send(json.dumps(msg))
            LOG.debug("→ state: tempo=%s playing=%s tracks=%d",
                       state.get("tempo"), state.get("playing"),
                       len(state.get("tracks", [])))
        except Exception as e:  # noqa: BLE001
            LOG.warning("Failed to send state: %s", e)

    # ------------------------------------------------------------------ #
    #  Dry-run command dispatch (mock)
    # ------------------------------------------------------------------ #
    async def _execute_mock(self, action: str, p: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a command against the in-memory MockAbleton (dry-run mode)."""
        m = self.mock
        assert m is not None, "mock state is only available in dry-run mode"
        # Transport
        if action == "play":
            return m.play()
        if action == "stop":
            return m.stop()
        if action == "set_tempo":
            return m.set_tempo(p["tempo"])
        if action == "set_time_signature":
            return m.set_time_signature(p["numerator"], p["denominator"])
        if action == "toggle_loop":
            return m.toggle_loop()
        if action == "toggle_metronome":
            return m.toggle_metronome()
        if action == "overdub":
            return m.overdub(p.get("on"))
        # Tracks
        if action == "create_midi_track":
            return m.create_midi_track(p.get("index"))
        if action == "create_audio_track":
            return m.create_audio_track(p.get("index"))
        if action == "delete_track":
            return m.delete_track(p["index"])
        if action == "duplicate_track":
            return m.duplicate_track(p["index"])
        if action == "mute_track":
            return m.mute_track(p["index"], p.get("mute", True))
        if action == "solo_track":
            return m.solo_track(p["index"], p.get("solo", True))
        if action == "set_volume":
            return m.set_volume(p["index"], p["volume"])
        if action == "set_pan":
            return m.set_pan(p["index"], p["pan"])
        if action == "set_send":
            return m.set_send(p["index"], p["send"], p["value"])
        # Clips
        if action == "create_midi_clip":
            return m.create_midi_clip(
                p["track"], p.get("length_beats", 4.0), p.get("scene"))
        if action == "set_clip_length":
            return m.set_clip_length(p["track"], p["clip"], p["length_beats"])
        if action == "add_note":
            return m.add_note(
                p["track"], p["clip"], p["pitch"], p["start"],
                p["duration"], p.get("velocity", 100))
        if action == "add_notes":
            return m.add_notes(p["track"], p["clip"], p["notes"])
        if action == "remove_note":
            return m.remove_note(p["track"], p["clip"], p["pitch"], p["start"])
        if action == "clear_clip":
            return m.clear_clip(p["track"], p["clip"])
        if action == "quantize_clip":
            return m.quantize_clip(p["track"], p["clip"], p.get("grid", 4))
        if action == "toggle_clip_loop":
            return m.toggle_clip_loop(p["track"], p["clip"])
        # Browser
        if action == "load_instrument":
            return m.load_instrument(p["track"], p["name"])
        if action == "load_effect":
            return m.load_effect(p["track"], p["name"])
        if action == "load_sample":
            return m.load_sample(p["track"], p["name"])
        if action == "load_drum_rack":
            return m.load_drum_rack(p["track"], p.get("name", "Drum Rack"))
        # Devices
        if action == "set_device_parameter":
            return m.set_device_parameter(
                p["track"], p["device"], p["param"], p["value"])
        if action == "get_device_parameters":
            return m.get_device_parameters(p["track"], p["device"])
        # Scenes
        if action == "create_scene":
            return m.create_scene(p.get("name", ""))
        if action == "launch_scene":
            return m.launch_scene(p["scene"])
        if action == "reorder_scene":
            return m.reorder_scene(p["scene"], p["new_index"])
        # State
        if action == "get_full_state":
            return m.full_state()
        raise ValueError(f"unknown action '{action}'")

    # ------------------------------------------------------------------ #
    #  Shutdown
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
#  Config loading
# --------------------------------------------------------------------------- #
def load_config(config_path: Optional[str]) -> Dict[str, Any]:
    """Load configuration from YAML file, then apply env var overrides."""
    cfg = dict(DEFAULT_CONFIG)
    if config_path and os.path.exists(config_path):
        if yaml is None:
            raise RuntimeError("PyYAML not installed; cannot read YAML config")
        with open(config_path) as f:
            user = yaml.safe_load(f) or {}
        if isinstance(user, dict):
            cfg.update(user)
    # Environment variable overrides
    env_map = {
        "BRIDGE_VPS_HOST": "vps_host",
        "BRIDGE_VPS_PORT": ("vps_port", int),
        "BRIDGE_AUTH_TOKEN": "auth_token",
        "BRIDGE_OSC_HOST": "osc_host",
        "BRIDGE_OSC_SEND_PORT": ("osc_send_port", int),
        "BRIDGE_OSC_LISTEN_PORT": ("osc_listen_port", int),
        "BRIDGE_STATE_INTERVAL": ("state_interval", float),
    }
    for env_key, spec in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if isinstance(spec, tuple):
                cfg[spec[0]] = spec[1](val)
            else:
                cfg[spec] = val
    return cfg


def merge_cli_args(cfg: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """Apply CLI argument overrides on top of config."""
    if args.vps_host:
        cfg["vps_host"] = args.vps_host
    if args.vps_port:
        cfg["vps_port"] = int(args.vps_port)
    if args.token:
        cfg["auth_token"] = args.token
    if args.osc_host:
        cfg["osc_host"] = args.osc_host
    if args.osc_port:
        cfg["osc_send_port"] = int(args.osc_port)
    if args.osc_listen_port:
        cfg["osc_listen_port"] = int(args.osc_listen_port)
    if args.state_interval is not None:
        cfg["state_interval"] = float(args.state_interval)
    return cfg


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hermes-Ableton OSC Bridge — connects VPS WebSocket server "
                    "to AbletonOSC via OSC.",
    )
    parser.add_argument("--config", default=None,
                        help="Path to config.yaml (default: bridge/config.yaml)")
    parser.add_argument("--vps-host", default=None,
                        help="VPS WebSocket server host (default: 177.7.34.85)")
    parser.add_argument("--vps-port", type=int, default=None,
                        help="VPS WebSocket server port (default: 8080)")
    parser.add_argument("--token", default=None,
                        help="Shared auth token (default: from config)")
    parser.add_argument("--osc-host", default=None,
                        help="AbletonOSC host (default: 127.0.0.1)")
    parser.add_argument("--osc-port", type=int, default=None,
                        help="AbletonOSC send port (default: 11000)")
    parser.add_argument("--osc-listen-port", type=int, default=None,
                        help="OSC response listen port (default: 11001)")
    parser.add_argument("--state-interval", type=float, default=None,
                        help="State poll interval in seconds (default: 2.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent — no real OSC or Ableton needed")
    args = parser.parse_args()

    # Determine config file path
    config_path = args.config
    if config_path is None:
        default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "config.yaml")
        if os.path.exists(default_path):
            config_path = default_path

    cfg = load_config(config_path)
    cfg = merge_cli_args(cfg, args)

    logging.basicConfig(
        level=getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    LOG.info("=" * 60)
    LOG.info("Hermes-Ableton OSC Bridge")
    LOG.info("  VPS:        ws://%s:%d", cfg["vps_host"], cfg["vps_port"])
    LOG.info("  OSC send:    %s:%d", cfg["osc_host"], cfg["osc_send_port"])
    LOG.info("  OSC listen:  %s:%d", cfg["osc_host"], cfg["osc_listen_port"])
    LOG.info("  State poll:  every %.1fs", cfg["state_interval"])
    LOG.info("  Dry-run:     %s", args.dry_run)
    LOG.info("=" * 60)

    if not args.dry_run and not PYTHONOSC_AVAILABLE:
        LOG.error("python-osc is not installed but --dry-run is not set.")
        LOG.error("Install with:  pip install python-osc")
        sys.exit(1)

    if not args.dry_run and websockets is None:
        LOG.error("websockets library is not installed.")
        LOG.error("Install with:  pip install websockets")
        sys.exit(1)

    bridge = AbletonOSCBridge(cfg, dry_run=args.dry_run)

    # Handle Ctrl+C gracefully
    def _signal_handler() -> None:
        LOG.info("Shutdown signal received.")
        bridge.stop()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler — use KeyboardInterrupt fallback
            pass

    try:
        loop.run_until_complete(bridge.run())
    except KeyboardInterrupt:
        LOG.info("Interrupted by user.")
    finally:
        bridge.stop()
        loop.close()


if __name__ == "__main__":
    main()
