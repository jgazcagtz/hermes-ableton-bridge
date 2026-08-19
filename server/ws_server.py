#!/usr/bin/env python3
"""
Hermes-Ableton Bridge — WebSocket Server
=========================================

Runs on the VPS (Linux). Bridges the Hermes agent (localhost HTTP, default :8081)
with Ableton Live on Windows (WebSocket, :8080).

Protocol updates in this version:
  - command execution is curated through explicit tools (no passthrough)
  - loopback-bound HTTP with mandatory bridge token
  - tool manifest at /tools/ableton/manifest
  - idempotency key replay for write calls
  - request/decision audit entries in-memory
  - scheduled plan execution support
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ipaddress
import signal
import ssl
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

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
    "http_host": "127.0.0.1",
    "http_port": 8081,
    "auth_token": "",
    "ssl": {"enabled": False, "cert": "", "key": ""},
    "state_interval": 2.0,
    "command_timeout": 10.0,
    "idempotency_ttl_seconds": 600,
    "idempotency_cache_size": 1024,
    "log_level": "INFO",
    "allowed_clients": [],
}

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback_address(addr: Optional[str]) -> bool:
    if not addr:
        return False
    if addr in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(addr).is_loopback
    except Exception:
        return False

TOOL_MANIFEST = {
    "version": "1.2.0",
    "generated_at": "",  # filled at runtime
    "tools": {
        "ableton_status": {
            "id": "ableton_status_v1",
            "execution_mode": "query",
            "risk": "low",
            "description": "Read connection status and cached Ableton state.",
            "params": {
                "fresh": "bool (optional)",
            },
            "supports_confirmation": False,
        },
        "ableton_transport": {
            "id": "ableton_transport_v1",
            "execution_mode": "execute",
            "risk": "medium",
            "description": (
                "Transport control: action='play'|'stop'|'loop'|'record'."
            ),
            "params": {"action": ["play", "stop", "loop", "record"], "on": "bool"},
            "supports_confirmation": False,
        },
        "ableton_set_tempo": {
            "id": "ableton_set_tempo_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Set Ableton tempo in BPM.",
            "params": {"tempo": "float(20..999)"},
            "supports_confirmation": False,
        },
        "ableton_get_timing": {
            "id": "ableton_get_timing_v1",
            "execution_mode": "query",
            "risk": "low",
            "description": "Read timing/state fields for bar-aware scheduling.",
            "params": {},
            "supports_confirmation": False,
        },
        "ableton_create_track": {
            "id": "ableton_create_track_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Create a new track (MIDI or audio).",
            "params": {
                "track_type": "midi|audio",
                "index": "int (optional)",
            },
            "supports_confirmation": False,
        },
        "ableton_set_track_level": {
            "id": "ableton_set_track_level_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Set track level properties (volume/pan/mute/solo).",
            "params": {
                "track": "int",
                "volume": "float(-60..6) [optional]",
                "pan": "float(-1..1) [optional]",
                "mute": "bool [optional]",
                "solo": "bool [optional]",
            },
            "supports_confirmation": False,
        },
        "ableton_create_clip": {
            "id": "ableton_create_clip_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Create a MIDI clip in the selected track/slot.",
            "params": {
                "track": "int",
                "length_beats": "float (>0)",
                "scene": "int [optional]",
            },
            "supports_confirmation": False,
        },
        "ableton_replace_clip_notes": {
            "id": "ableton_replace_clip_notes_v1",
            "execution_mode": "execute",
            "risk": "high",
            "description": "Replace all notes in a clip with a batched note list.",
            "params": {
                "track": "int",
                "clip": "int",
                "notes": "list<{pitch,start,duration,velocity}>",
                "confirm": "bool",
            },
            "supports_confirmation": True,
        },
        "ableton_launch_scene": {
            "id": "ableton_launch_scene_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Launch a scene by index.",
            "params": {"scene": "int"},
            "supports_confirmation": False,
        },
        "ableton_launch_clip": {
            "id": "ableton_launch_clip_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Launch a clip slot by track/clip index.",
            "params": {"track": "int", "clip": "int"},
            "supports_confirmation": False,
        },
        "ableton_clear_clip": {
            "id": "ableton_clear_clip_v1",
            "execution_mode": "execute",
            "risk": "medium",
            "description": "Clear clip notes in a track/clip slot.",
            "params": {"track": "int", "clip": "int"},
            "supports_confirmation": True,
        },
        "ableton_remove_clip_note": {
            "id": "ableton_remove_clip_note_v1",
            "execution_mode": "execute",
            "risk": "high",
            "description": "Remove a single note from a track/clip slot.",
            "params": {
                "track": "int",
                "clip": "int",
                "pitch": "int (0..127)",
                "start": "float (>=0)",
                "confirm": "bool",
            },
            "supports_confirmation": True,
        },
        "ableton_set_clip_loop": {
            "id": "ableton_set_clip_loop_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Turn loop mode on/off in a track/clip slot.",
            "params": {"track": "int", "clip": "int", "loop": "bool"},
            "supports_confirmation": False,
        },
        "ableton_toggle_clip_loop": {
            "id": "ableton_toggle_clip_loop_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Toggle loop mode for a track/clip slot.",
            "params": {"track": "int", "clip": "int"},
            "supports_confirmation": False,
        },
        "ableton_stop_clip": {
            "id": "ableton_stop_clip_v1",
            "execution_mode": "execute",
            "risk": "low",
            "description": "Stop a running clip slot in a track.",
            "params": {"track": "int", "clip": "int"},
            "supports_confirmation": False,
        },
        "ableton_schedule_plan": {
            "id": "ableton_schedule_plan_v1",
            "execution_mode": "queue",
            "risk": "medium",
            "description": (
                "Replace clip notes on bar boundary (or immediately) for one "
                "or more track/clip targets."
            ),
            "params": {
                "alignment": "next_bar|next_beat|immediate",
                "tracks": "[{track, clip, notes}]",
                "preview": "bool [optional]",
                "plan_id": "string [optional]",
                "confirm": "bool [optional]",
            },
            "supports_confirmation": False,
        },
    },
    "unsupported": {
        "/raw": {
            "id": "raw_v1",
            "risk": "critical",
            "reason": "Arbitrary JSON forwarding removed.",
        },
        "load_instrument": {
            "id": "load_instrument_v1",
            "risk": "high",
            "reason": "Browser-driven load-by-name flow is manual and unreliable.",
        },
        "load_effect": {
            "id": "load_effect_v1",
            "risk": "high",
            "reason": "Browser-driven load-by-name flow is manual and unreliable.",
        },
        "load_sample": {
            "id": "load_sample_v1",
            "risk": "high",
            "reason": "Browser-driven load-by-name flow is manual and unreliable.",
        },
        "load_drum_rack": {
            "id": "load_drum_rack_v1",
            "risk": "high",
            "reason": "Browser-driven load-by-name flow is manual and unreliable.",
        },
        "set_device_parameter": {
            "id": "set_device_parameter_v1",
            "risk": "high",
            "reason": "Not part of curated v1 ableton transport/musical tools.",
        },
        "remove_note": {
            "id": "remove_note_v1",
            "risk": "high",
            "reason": "Use replace_clip_notes for deterministic clip edits.",
        },
        "add_note": {
            "id": "add_note_v1",
            "risk": "high",
            "reason": "Use replace_clip_notes for deterministic clip edits.",
        },
        "add_notes": {
            "id": "add_notes_v1",
            "risk": "high",
            "reason": "Use replace_clip_notes for deterministic clip edits.",
        },
        "quantize_clip": {
            "id": "quantize_clip_v1",
            "risk": "medium",
            "reason": "Advanced timing math is behind explicit scheduling actions.",
        },
        "set_clip_length": {
            "id": "set_clip_length_v1",
            "risk": "medium",
            "reason": "Not exposed in v1 deterministic note-edit contract.",
        },
        "create_scene": {
            "id": "create_scene_v1",
            "risk": "medium",
            "reason": "Scene creation intentionally left out of v1 action surface.",
        },
        "get_full_state": {
            "id": "get_full_state_v1",
            "risk": "low",
            "reason": "Use ableton_status(fresh=true) for state reads.",
        },
    },
}

SUPPORTED_TOOL_TO_ACTION = {
    "ableton_status": "get_full_state",
    "ableton_set_tempo": "set_tempo",
    "ableton_get_timing": "get_full_state",
    "ableton_create_track": {
        "midi": "create_midi_track",
        "audio": "create_audio_track",
    },
    "ableton_set_track_level": {
        "volume": "set_volume",
        "pan": "set_pan",
        "mute": "mute_track",
        "solo": "solo_track",
    },
    "ableton_create_clip": "create_midi_clip",
    "ableton_replace_clip_notes": "replace_clip_notes",
    "ableton_launch_scene": "launch_scene",
    "ableton_launch_clip": "launch_clip",
    "ableton_clear_clip": "clear_clip",
    "ableton_remove_clip_note": "remove_note",
    "ableton_set_clip_loop": "set_clip_loop",
    "ableton_toggle_clip_loop": "toggle_clip_loop",
    "ableton_stop_clip": "stop_clip",
}

ALLOWED_TOOLS = set(TOOL_MANIFEST["tools"])

# Legacy action support for existing clients, intentionally narrow and explicit.
LEGACY_TOOL_MAP = {
    "play": ("ableton_transport", {"action": "play"}),
    "stop": ("ableton_transport", {"action": "stop"}),
    "set_tempo": ("ableton_set_tempo", {}),
    "toggle_loop": ("ableton_transport", {"action": "loop"}),
    "create_midi_track": ("ableton_create_track", {"track_type": "midi"}),
    "create_audio_track": ("ableton_create_track", {"track_type": "audio"}),
    "mute_track": ("ableton_set_track_level", {}),
    "solo_track": ("ableton_set_track_level", {}),
    "set_volume": ("ableton_set_track_level", {}),
    "set_pan": ("ableton_set_track_level", {}),
    "create_midi_clip": ("ableton_create_clip", {}),
    "launch_scene": ("ableton_launch_scene", {}),
    "launch_clip": ("ableton_launch_clip", {}),
    "clear_clip": ("ableton_clear_clip", {"confirm": True}),
    "get_full_state": ("ableton_status", {"fresh": True}),
    "replace_clip_notes": ("ableton_replace_clip_notes", {"confirm": True}),
}

UNSUPPORTED_LEGACY_ACTIONS = {
    "load_instrument",
    "load_effect",
    "load_sample",
    "load_drum_rack",
    "set_device_parameter",
    "get_device_parameters",
    "remove_note",
    "quantize_clip",
    "toggle_clip_loop",
    "set_clip_length",
    "toggle_metronome",
    "overdub",
    "delete_track",
    "duplicate_track",
    "reorder_scene",
    "create_scene",
    "remove_note",
    "add_note",
    "add_notes",
}


class BridgeState:
    """Holds the single Ableton WS connection, last known state, and pending commands."""

    def __init__(self, token: str, command_timeout: float,
                 idempotency_ttl_seconds: int, idempotency_cache_size: int):
        self.token = token
        self.command_timeout = command_timeout
        self.ableton_ws: Optional[websockets.WebSocketServerProtocol] = None
        self.last_state: Dict[str, Any] = {}
        self.pending: Dict[str, "asyncio.Future[Any]"] = {}
        self.lock = asyncio.Lock()
        self._idempotency: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._idempotency_ttl = float(idempotency_ttl_seconds)
        self._idempotency_cache_size = int(idempotency_cache_size)
        self.audit_log: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._scheduled_plans: Dict[str, asyncio.Task[Any]] = {}
        self._plan_previews: Dict[str, Dict[str, Any]] = {}

    @property
    def connected(self) -> bool:
        if self.ableton_ws is None:
            return False
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
            if isinstance(result, dict) and "data" in result:
                return result["data"] or {}
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
        if fut.done():
            return
        if msg.get("status") == "ok":
            fut.set_result(msg)
        else:
            fut.set_exception(RuntimeError(msg.get("error") or "Ableton returned an error"))

    def update_state(self, data: Dict[str, Any]):
        self.last_state = data
        self.last_state["_updated_at"] = int(time.time())

    def read_audit(self) -> List[Dict[str, Any]]:
        return list(self.audit_log)

    def record_audit(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event["timestamp"] = int(time.time())
        self.audit_log.appendleft(event)

    def _prune_idempotency(self, now: float) -> None:
        expired = [k for k, v in self._idempotency.items() if now > v[0] + self._idempotency_ttl]
        for key in expired:
            self._idempotency.pop(key, None)

    def get_idempotent(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        self._prune_idempotency(now)
        hit = self._idempotency.get(key)
        if hit is None:
            return None
        if now > hit[0] + self._idempotency_ttl:
            self._idempotency.pop(key, None)
            return None
        return hit[1]

    def set_idempotent(self, key: str, response: Dict[str, Any]) -> None:
        now = time.time()
        self._prune_idempotency(now)
        if len(self._idempotency) >= self._idempotency_cache_size:
            oldest = sorted(self._idempotency.items(), key=lambda kv: kv[1][0])[:1]
            for stale_key, _ in oldest:
                self._idempotency.pop(stale_key, None)
        self._idempotency[key] = (now, response)

    def track_scheduled_plan(self, plan_id: str, task: asyncio.Task[Any]) -> None:
        self._scheduled_plans[plan_id] = task

    def complete_scheduled_plan(self, plan_id: str) -> None:
        self._scheduled_plans.pop(plan_id, None)

    def snapshot_clip(self, track_index: int, clip_index: int) -> Optional[Dict[str, Any]]:
        tracks = self.last_state.get("tracks")
        if not isinstance(tracks, list):
            return None
        for track in tracks:
            if not isinstance(track, dict):
                continue
            if track.get("index") != track_index:
                continue
            clips = track.get("clips")
            if not isinstance(clips, list):
                return None
            for clip in clips:
                if not isinstance(clip, dict):
                    continue
                if clip.get("index") != clip_index:
                    continue
                return {
                    "track": track_index,
                    "clip": clip_index,
                    "notes": list(clip.get("notes", []) or []),
                }
        return None


def _require_token(cfg: Dict[str, Any]) -> None:
    token = (cfg.get("auth_token") or "").strip()
    if not token or token == "change-me-please":
        raise RuntimeError(
            "auth_token must be set to a runtime-loaded value (env/config), "
            "not the default placeholder."
        )


def _ensure_loopback_host(cfg: Dict[str, Any]) -> None:
    http_host = cfg.get("http_host", "127.0.0.1")
    if not _is_loopback_address(str(http_host)):
        raise RuntimeError(
            "HTTP API must be loopback-bound for safety. "
            "Set http_host to 127.0.0.1 (default)."
        )


def _to_str(obj: Any) -> str:
    return str(obj) if obj is not None else ""


def _as_int(name: str, value: Any, min_value: Optional[int] = None,
            max_value: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if min_value is not None and parsed < min_value:
        raise ValueError(f"{name} must be >= {min_value}")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{name} must be <= {max_value}")
    return parsed


def _as_float(name: str, value: Any, min_value: Optional[float] = None,
              max_value: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if min_value is not None and parsed < min_value:
        raise ValueError(f"{name} must be >= {min_value}")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{name} must be <= {max_value}")
    return parsed


def _as_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"{name} must be boolean")


def _validate_note(note: Any, index: int) -> Dict[str, Any]:
    if not isinstance(note, dict):
        raise ValueError(f"note[{index}] must be an object")
    pitch = _as_int("note.pitch", note.get("pitch"), 0, 127)
    start = _as_float("note.start", note.get("start"), min_value=0.0)
    duration = _as_float("note.duration", note.get("duration"), min_value=0.0)
    velocity = _as_int("note.velocity", note.get("velocity"), 0, 127)
    return {
        "pitch": pitch,
        "start": start,
        "duration": duration,
        "velocity": velocity,
    }


def _validate_tool_payload(tool: str, params: Dict[str, Any]) -> Dict[str, Any]:
    norm: Dict[str, Any] = {}

    if tool == "ableton_status":
        if "fresh" in params:
            norm["fresh"] = _as_bool("ableton_status.fresh", params["fresh"])
        return norm

    if tool == "ableton_transport":
        action = _to_str(params.get("action")).strip().lower()
        if action not in {"play", "stop", "loop", "record"}:
            raise ValueError("ableton_transport.action must be play, stop, loop, or record")
        if action == "record":
            if "on" not in params:
                raise ValueError("ableton_transport.action='record' requires on")
            norm["on"] = _as_bool("ableton_transport.on", params["on"])
        elif action == "loop":
            if "on" in params:
                norm["on"] = _as_bool("ableton_transport.on", params["on"])
        norm["action"] = action
        return norm

    if tool == "ableton_set_tempo":
        norm["tempo"] = _as_float("tempo", params.get("tempo"), 20.0, 999.0)
        return norm

    if tool == "ableton_get_timing":
        return norm

    if tool == "ableton_create_track":
        track_type = _to_str(params.get("track_type")).strip().lower()
        if track_type not in {"midi", "audio"}:
            raise ValueError("ableton_create_track.track_type must be 'midi' or 'audio'")
        norm["track_type"] = track_type
        if "index" in params and params["index"] is not None:
            norm["index"] = _as_int("index", params["index"], min_value=0)
        return norm

    if tool == "ableton_set_track_level":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        updates = 0
        if "volume" in params:
            norm["volume"] = _as_float("volume", params["volume"], -60.0, 6.0)
            updates += 1
        if "pan" in params:
            norm["pan"] = _as_float("pan", params["pan"], -1.0, 1.0)
            updates += 1
        if "mute" in params:
            norm["mute"] = _as_bool("mute", params["mute"])
            updates += 1
        if "solo" in params:
            norm["solo"] = _as_bool("solo", params["solo"])
            updates += 1
        if updates < 1:
            raise ValueError(
                "ableton_set_track_level requires at least one of volume, pan, mute, solo"
            )
        return norm

    if tool == "ableton_create_clip":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        if "length_beats" in params:
            norm["length_beats"] = _as_float("length_beats", params["length_beats"], 0.001)
        else:
            norm["length_beats"] = 4.0
        if "scene" in params:
            norm["scene"] = _as_int("scene", params["scene"], min_value=0)
        return norm

    if tool == "ableton_replace_clip_notes":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        norm["clip"] = _as_int("clip", params.get("clip"), min_value=0)
        notes = params.get("notes", [])
        if not isinstance(notes, list):
            raise ValueError("ableton_replace_clip_notes.notes must be a list")
        norm["notes"] = [_validate_note(note, idx) for idx, note in enumerate(notes)]
        # Confirmation is explicit for destructive operations.
        if "confirm" in params and not isinstance(params["confirm"], bool):
            raise ValueError("ableton_replace_clip_notes.confirm must be a boolean")
        norm["confirm"] = bool(params.get("confirm", False))
        return norm

    if tool == "ableton_launch_scene":
        norm["scene"] = _as_int("scene", params.get("scene"), min_value=0)
        return norm

    if tool == "ableton_launch_clip":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        norm["clip"] = _as_int("clip", params.get("clip"), min_value=0)
        return norm

    if tool == "ableton_clear_clip":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        norm["clip"] = _as_int("clip", params.get("clip"), min_value=0)
        if "confirm" in params and not isinstance(params["confirm"], bool):
            raise ValueError("ableton_clear_clip.confirm must be a boolean")
        norm["confirm"] = bool(params.get("confirm", False))
        return norm

    if tool == "ableton_remove_clip_note":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        norm["clip"] = _as_int("clip", params.get("clip"), min_value=0)
        norm["pitch"] = _as_int("pitch", params.get("pitch"), min_value=0, max_value=127)
        norm["start"] = _as_float("start", params.get("start"), min_value=0.0)
        if "confirm" in params and not isinstance(params["confirm"], bool):
            raise ValueError("ableton_remove_clip_note.confirm must be a boolean")
        norm["confirm"] = bool(params.get("confirm", False))
        return norm

    if tool == "ableton_set_clip_loop":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        norm["clip"] = _as_int("clip", params.get("clip"), min_value=0)
        if "loop" not in params:
            raise ValueError("ableton_set_clip_loop requires loop=true|false")
        norm["loop"] = _as_bool("ableton_set_clip_loop.loop", params["loop"])
        return norm

    if tool == "ableton_toggle_clip_loop":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        norm["clip"] = _as_int("clip", params.get("clip"), min_value=0)
        return norm

    if tool == "ableton_stop_clip":
        norm["track"] = _as_int("track", params.get("track"), min_value=0)
        norm["clip"] = _as_int("clip", params.get("clip"), min_value=0)
        return norm

    if tool == "ableton_schedule_plan":
        norm["alignment"] = _to_str(params.get("alignment")).strip().lower() or "next_bar"
        if norm["alignment"] not in {"next_bar", "next_beat", "immediate"}:
            raise ValueError(
                "ableton_schedule_plan.alignment must be next_bar, next_beat, or immediate"
            )
        tracks = params.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            raise ValueError("ableton_schedule_plan.tracks must be a non-empty list")
        norm_tracks = []
        for entry_index, entry in enumerate(tracks):
            if not isinstance(entry, dict):
                raise ValueError(f"tracks[{entry_index}] must be an object")
            t = _as_int("tracks[].track", entry.get("track"), min_value=0)
            c = _as_int("tracks[].clip", entry.get("clip"), min_value=0)
            notes = entry.get("notes", [])
            if not isinstance(notes, list):
                raise ValueError(f"tracks[{entry_index}].notes must be a list")
            norm_tracks.append({
                "track": t,
                "clip": c,
                "notes": [_validate_note(note, i) for i, note in enumerate(notes)],
            })
        norm["tracks"] = norm_tracks
        norm["preview"] = bool(params.get("preview", False))
        norm["plan_id"] = _to_str(params.get("plan_id")) if params.get("plan_id") is not None else None
        if "confirm" in params and not isinstance(params["confirm"], bool):
            raise ValueError("ableton_schedule_plan.confirm must be a boolean")
        norm["confirm"] = bool(params.get("confirm", False))
        return norm

    raise ValueError(f"unsupported tool '{tool}'")


def _tool_from_request(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if "tool" in payload:
        tool = _to_str(payload.get("tool")).strip()
        if not tool:
            raise ValueError("tool is required")
        if tool not in TOOL_MANIFEST["tools"] and tool not in TOOL_MANIFEST["unsupported"]:
            raise ValueError(f"tool '{tool}' is not in manifest")
        if tool in TOOL_MANIFEST["unsupported"]:
            raise ValueError(
                f"tool '{tool}' is unsupported: "
                f"{TOOL_MANIFEST['unsupported'][tool].get('reason', 'not available in v1 API')}"
            )
        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        return tool, params

    action = _to_str(payload.get("action")).strip()
    if not action:
        raise ValueError("tool or action is required")
    if action in UNSUPPORTED_LEGACY_ACTIONS:
        if action in TOOL_MANIFEST["unsupported"]:
            raise ValueError(
                f"legacy action '{action}' is unsupported: "
                f"{TOOL_MANIFEST['unsupported'][action].get('reason', 'not available in v1 API')}"
            )
        raise ValueError(f"legacy action '{action}' is no longer supported")
    mapping = LEGACY_TOOL_MAP.get(action)
    if mapping is None:
        raise ValueError(f"legacy action '{action}' is not supported")
    tool, extra = mapping
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    merged = dict(params)
    merged.update(extra)
    return tool, merged


def _payload_token(request: web.Request, state: BridgeState,
                  allowed_client: Optional[List[str]] = None) -> Tuple[bool, Optional[web.Response]]:
    if request.remote is not None and not _is_loopback_address(request.remote):
        state.record_audit({
            "tool": "http",
            "mode": request.method,
            "client": request.remote,
            "decision": "rejected",
            "error": "caller is not loopback",
            "path": request.path,
        })
        return False, web.json_response(
            {
                "status": "rejected",
                "decision": "rejected",
                "error": "caller must be loopback",
            },
            status=403,
        )
    allowlist = allowed_client or []
    if allowlist and request.remote not in allowlist:
        state.record_audit({
            "tool": "http",
            "mode": request.method,
            "client": request.remote,
            "decision": "rejected",
            "error": "caller is not authorized",
            "path": request.path,
        })
        return False, web.json_response(
            {
                "status": "rejected",
                "decision": "rejected",
                "error": "caller is not authorized",
            },
            status=403,
        )

    header_token = request.headers.get("X-Bridge-Token", "")
    if header_token == state.token:
        return True, None
    state.record_audit({
        "tool": "http",
        "mode": request.method,
        "client": request.remote,
        "decision": "rejected",
        "error": "missing or invalid X-Bridge-Token",
        "path": request.path,
    })
    return False, web.json_response(
        {
            "status": "rejected",
            "decision": "requires_auth",
            "error": "missing or invalid X-Bridge-Token",
        },
        status=401,
    )


def _compute_beats_to_next_boundary(state: Dict[str, Any], alignment: str) -> float:
    if alignment == "next_beat":
        beat_position = state.get("beat_position")
        if not isinstance(beat_position, (int, float)):
            beat_position = 0.0
        try:
            tempo = float(state.get("tempo", 120.0))
        except (TypeError, ValueError):
            tempo = 120.0
        beat_in_subdivision = float(beat_position) % 1.0
        remaining = 0.0 if abs(beat_in_subdivision) < 1e-6 else 1.0 - beat_in_subdivision
        return (60.0 / max(tempo, 1.0)) * remaining
    if alignment != "next_bar":
        return 0.0
    tempo = float(state.get("tempo", 120.0))
    ts = state.get("time_signature")
    beats_per_bar = 4
    if isinstance(ts, Sequence) and len(ts) >= 1:
        try:
            beats_per_bar = max(1, int(ts[0]))
        except (TypeError, ValueError):
            beats_per_bar = 4
    beat_position = state.get("beat_position")
    if not isinstance(beat_position, (int, float)):
        beat_position = 0.0
    beat_in_bar = float(beat_position) % beats_per_bar if beats_per_bar else 0.0
    remaining = 0.0 if beat_in_bar == 0.0 else (beats_per_bar - beat_in_bar)
    return (60.0 / tempo) * remaining


async def _safe_state(state: BridgeState) -> Dict[str, Any]:
    try:
        return await state.send_command("get_full_state")
    except Exception:
        return {}


def _find_track(snapshot: Dict[str, Any], track_index: int) -> Optional[Dict[str, Any]]:
    tracks = snapshot.get("tracks")
    if not isinstance(tracks, list):
        return None
    for track in tracks:
        if isinstance(track, dict) and int(track.get("index", -1)) == int(track_index):
            return track
    return None


def _find_clip(snapshot_track: Dict[str, Any], clip_index: int) -> Optional[Dict[str, Any]]:
    clips = snapshot_track.get("clips")
    if not isinstance(clips, list):
        return None
    for clip in clips:
        if isinstance(clip, dict) and int(clip.get("index", -1)) == int(clip_index):
            return clip
    return None


async def _snapshot_after_action(state: BridgeState, tool: str,
                                params: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = await _safe_state(state)
    if not snapshot:
        return {}
    if tool in {"ableton_set_tempo"}:
        return {"tempo": snapshot.get("tempo")}
    if tool == "ableton_get_timing":
        beats_per_bar = 4
        time_signature = snapshot.get("time_signature")
        if isinstance(time_signature, Sequence) and len(time_signature) >= 1:
            try:
                beats_per_bar = max(1, int(time_signature[0]))
            except (TypeError, ValueError):
                beats_per_bar = 4
        beat_position = float(snapshot.get("beat_position", 0.0))
        beat_within_bar = beat_position % beats_per_bar if beats_per_bar else beat_position
        next_bar_offset = 0.0 if abs(beat_within_bar) < 1e-6 else beats_per_bar - beat_within_bar
        next_beat_offset = 0.0 if abs(beat_position % 1.0) < 1e-6 else 1.0 - (beat_position % 1.0)
        return {
            "tempo": snapshot.get("tempo"),
            "time_signature": time_signature,
            "beat_position": beat_position,
            "beat_within_bar": beat_within_bar,
            "next_bar_in_beats": next_bar_offset,
            "next_beat_in_beats": next_beat_offset,
        }
    if tool == "ableton_transport":
        return {
            "playing": snapshot.get("playing"),
            "loop": snapshot.get("loop"),
            "overdub": snapshot.get("overdub"),
        }
    if tool == "ableton_status" and params.get("fresh"):
        return {"tracks": snapshot.get("tracks"), "scenes": snapshot.get("scenes")}
    if tool in {"ableton_create_track", "ableton_create_clip", "ableton_launch_scene",
                "ableton_launch_clip", "ableton_set_track_level",
                "ableton_replace_clip_notes", "ableton_clear_clip",
                "ableton_set_clip_loop", "ableton_toggle_clip_loop",
                "ableton_remove_clip_note", "ableton_stop_clip"}:
        track = _find_track(snapshot, params.get("track", -1)) if "track" in params else None
        data: Dict[str, Any] = {}
        if track is not None:
            data["track"] = {
                "index": track.get("index"),
                "volume": track.get("volume"),
                "pan": track.get("pan"),
                "mute": track.get("mute"),
                "solo": track.get("solo"),
            }
        if "clip" in params and track is not None:
            clip = _find_clip(track, params["clip"])
            if clip is not None:
                data["clip"] = {
                    "index": clip.get("index"),
                    "length_beats": clip.get("length_beats"),
                    "notes": clip.get("notes"),
                    "loop": clip.get("loop"),
                }
        return data
    return {}


async def _execute_tool(state: BridgeState, tool: str, params: Dict[str, Any],
                       client_ip: str) -> Dict[str, Any]:
    state.record_audit({
        "tool": tool,
        "mode": "execute",
        "client": client_ip,
    })

    if tool == "ableton_status":
        state_snapshot = state.last_state
        if params.get("fresh"):
            state_snapshot = await _safe_state(state)
        return {"connected": state.connected, "state": state_snapshot}

    if tool == "ableton_get_timing":
        state_snapshot = await _safe_state(state)
        if not state_snapshot:
            return {}
        beats_per_bar = 4
        time_signature = state_snapshot.get("time_signature")
        if isinstance(time_signature, Sequence) and len(time_signature) >= 1:
            try:
                beats_per_bar = max(1, int(time_signature[0]))
            except (TypeError, ValueError):
                beats_per_bar = 4
        beat_position = float(state_snapshot.get("beat_position", 0.0))
        beat_within_bar = beat_position % beats_per_bar if beats_per_bar else beat_position
        next_bar_offset = 0.0 if abs(beat_within_bar) < 1e-6 else beats_per_bar - beat_within_bar
        next_beat_offset = 0.0 if abs(beat_position % 1.0) < 1e-6 else 1.0 - (beat_position % 1.0)
        return {
            "tempo": state_snapshot.get("tempo"),
            "time_signature": time_signature,
            "beat_position": beat_position,
            "beat_within_bar": beat_within_bar,
            "next_bar_in_beats": next_bar_offset,
            "next_beat_in_beats": next_beat_offset,
        }

    if tool == "ableton_transport":
        action = params["action"]
        if action == "play":
            data = await state.send_command("play")
            verification = await _snapshot_after_action(state, tool, params)
            data["verification"] = verification
            return data
        if action == "stop":
            data = await state.send_command("stop")
            verification = await _snapshot_after_action(state, tool, params)
            data["verification"] = verification
            return data
        if action == "loop":
            if "on" in params:
                data = await state.send_command("set_loop", {"on": params["on"]})
                verification = await _snapshot_after_action(state, tool, params)
                data["verification"] = verification
                return data
            data = await state.send_command("toggle_loop")
            verification = await _snapshot_after_action(state, tool, params)
            data["verification"] = verification
            return data
        if action == "record":
            data = await state.send_command("overdub", {"on": params["on"]})
            verification = await _snapshot_after_action(state, tool, params)
            data["verification"] = verification
            return data
        raise ValueError(f"unknown transport action '{action}'")

    if tool == "ableton_set_tempo":
        data = await state.send_command("set_tempo", {"tempo": params["tempo"]})
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    if tool == "ableton_create_track":
        action = SUPPORTED_TOOL_TO_ACTION["ableton_create_track"][params["track_type"]]
        request = {"index": params.get("index")} if params.get("index") is not None else {}
        data = await state.send_command(action, request)
        data["verification"] = await _snapshot_after_action(state, tool, {
            "track": request.get("index", data.get("track", 0))
        })
        return data

    if tool == "ableton_set_track_level":
        track = params["track"]
        responses = []
        if "volume" in params:
            resp = await state.send_command("set_volume", {
                "index": track, "volume": params["volume"]
            })
            responses.append(resp)
        if "pan" in params:
            resp = await state.send_command("set_pan", {
                "index": track, "pan": params["pan"]
            })
            responses.append(resp)
        if "mute" in params:
            resp = await state.send_command("mute_track", {
                "index": track, "mute": params["mute"]
            })
            responses.append(resp)
        if "solo" in params:
            resp = await state.send_command("solo_track", {
                "index": track, "solo": params["solo"]
            })
            responses.append(resp)
        merged: Dict[str, Any] = {"track": track}
        for response in responses:
            merged.update(response)
        merged["verification"] = await _snapshot_after_action(state, tool, {"track": track})
        return merged

    if tool == "ableton_create_clip":
        payload = {"track": params["track"], "length_beats": params["length_beats"]}
        if "scene" in params:
            payload["scene"] = params["scene"]
        data = await state.send_command("create_midi_clip", payload)
        data["verification"] = await _snapshot_after_action(state, tool, payload)
        return data

    if tool == "ableton_replace_clip_notes":
        if not params.get("confirm", False):
            raise ValueError("requires_confirmation")
        data = await state.send_command("replace_clip_notes", {
            "track": params["track"],
            "clip": params["clip"],
            "notes": params["notes"],
        })
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    if tool == "ableton_remove_clip_note":
        if not params.get("confirm", False):
            raise ValueError("requires_confirmation")
        data = await state.send_command("remove_note", {
            "track": params["track"],
            "clip": params["clip"],
            "pitch": params["pitch"],
            "start": params["start"],
        })
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    if tool == "ableton_launch_scene":
        data = await state.send_command("launch_scene", {"scene": params["scene"]})
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    if tool == "ableton_launch_clip":
        data = await state.send_command("launch_clip", {
            "track": params["track"], "clip": params["clip"]
        })
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    if tool == "ableton_clear_clip":
        if not params.get("confirm", False):
            raise ValueError("requires_confirmation")
        data = await state.send_command("clear_clip", {
            "track": params["track"],
            "clip": params["clip"],
        })
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    if tool == "ableton_set_clip_loop":
        data = await state.send_command("set_clip_loop", {
            "track": params["track"],
            "clip": params["clip"],
            "loop": params["loop"],
        })
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    if tool == "ableton_toggle_clip_loop":
        data = await state.send_command("toggle_clip_loop", {
            "track": params["track"],
            "clip": params["clip"],
        })
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    if tool == "ableton_stop_clip":
        data = await state.send_command("stop_clip", {
            "track": params["track"],
            "clip": params["clip"],
        })
        data["verification"] = await _snapshot_after_action(state, tool, params)
        return data

    raise ValueError(f"unsupported tool '{tool}'")


async def _schedule_plan_task(state: BridgeState, plan_id: str, tool_plan: Dict[str, Any]) -> None:
    alignment = tool_plan["alignment"]
    preview_only = bool(tool_plan.get("preview", False))
    delay = _compute_beats_to_next_boundary(state.last_state, alignment)

    # A no-op preview returns the same envelope as a dry-run decision.
    if preview_only:
        state.record_audit({
            "tool": "ableton_schedule_plan",
            "plan_id": plan_id,
            "decision": "dry_run_preview",
            "delay_seconds": delay,
        })
        return

    try:
        if delay > 0:
            await asyncio.sleep(delay)
        pre: List[Dict[str, Any]] = []
        for entry in tool_plan["tracks"]:
            snapshot = state.snapshot_clip(entry["track"], entry["clip"])
            if snapshot is not None:
                pre.append(snapshot)
        if pre:
            state._plan_previews[plan_id] = {"tracks": pre}
        for entry in tool_plan["tracks"]:
            await state.send_command("replace_clip_notes", {
                "track": entry["track"],
                "clip": entry["clip"],
                "notes": entry["notes"],
            })
        state.record_audit({
            "tool": "ableton_schedule_plan",
            "plan_id": plan_id,
            "decision": "executed",
        })
    except Exception as exc:  # noqa: BLE001
        LOG.error("Plan execution failed: %s", exc)
        snapshot = state._plan_previews.get(plan_id)
        if snapshot:
            for entry in snapshot.get("tracks", []):
                try:
                    await state.send_command("replace_clip_notes", entry)
                except Exception as restore_exc:  # noqa: BLE001
                    state.record_audit({
                        "tool": "ableton_schedule_plan",
                        "plan_id": plan_id,
                        "decision": "rollback_failed",
                        "restore_error": str(restore_exc),
                    })
            state.record_audit({
                "tool": "ableton_schedule_plan",
                "plan_id": plan_id,
                "decision": "restored_snapshot",
            })
        state.record_audit({
            "tool": "ableton_schedule_plan",
            "plan_id": plan_id,
            "decision": "rejected",
            "error": str(exc),
        })
    finally:
        state._plan_previews.pop(plan_id, None)
        state.complete_scheduled_plan(plan_id)


async def _execute_or_queue_plan(state: BridgeState, params: Dict[str, Any], client_ip: str) -> Tuple[str, Dict[str, Any]]:
    state.record_audit({
        "tool": "ableton_schedule_plan",
        "mode": params.get("alignment", "next_bar"),
        "client": client_ip,
    })
    if not params.get("confirm", False):
        return "requires_confirmation", {
            "preview": bool(params.get("preview", False)),
            "commands": len(params["tracks"]),
        }
    plan_id = params.get("plan_id") or str(uuid.uuid4())
    delay = _compute_beats_to_next_boundary(state.last_state, params["alignment"])
    if bool(params.get("preview", False)):
        return (
            "dry_run_preview",
            {
                "plan_id": plan_id,
                "alignment": params["alignment"],
                "delay_seconds": delay,
                "commands": len(params["tracks"]),
            },
        )
    task = asyncio.create_task(_schedule_plan_task(state, plan_id, params))
    state.track_scheduled_plan(plan_id, task)
    return (
        "queued_for_bar",
        {"plan_id": plan_id, "alignment": params["alignment"], "delay_seconds": delay},
    )


async def ableton_ws_handler(ws, state: BridgeState):
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


async def http_command(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    ok, err = _payload_token(request, state,
                             request.app["config"].get("allowed_clients"))
    if not ok:
        return err

    try:
        body = await request.json()
    except json.JSONDecodeError:
        state.record_audit({
            "tool": "http",
            "mode": request.method,
            "client": request.remote,
            "decision": "rejected",
            "error": "invalid JSON body",
            "path": request.path,
        })
        return web.json_response({
            "status": "rejected",
            "decision": "rejected",
            "error": "invalid JSON body",
        }, status=400)
    if not isinstance(body, dict):
        state.record_audit({
            "tool": "http",
            "mode": request.method,
            "client": request.remote,
            "decision": "rejected",
            "error": "request body must be JSON object",
            "path": request.path,
        })
        return web.json_response({
            "status": "rejected",
            "decision": "rejected",
            "error": "request body must be JSON object",
        }, status=400)

    try:
        tool, params = _tool_from_request(body)
    except ValueError as exc:
        state.record_audit({
            "tool": body.get("tool") or "http",
            "mode": request.method,
            "client": request.remote,
            "decision": "rejected",
            "error": str(exc),
            "path": request.path,
        })
        return web.json_response({
            "status": "rejected",
            "decision": "rejected",
            "error": str(exc),
        }, status=400)

    try:
        params = _validate_tool_payload(tool, params)
    except ValueError as exc:
        state.record_audit({
            "tool": tool,
            "decision": "rejected",
            "error": str(exc),
            "client": request.remote,
            "path": request.path,
        })
        return web.json_response({
            "status": "error",
            "decision": "rejected",
            "error": str(exc),
        }, status=400)

    tool_meta = TOOL_MANIFEST["tools"].get(tool)
    if tool_meta is None:
        tool_meta = {}
    default_mode = "query" if tool_meta.get("execution_mode") == "query" else "execute"
    mode = _to_str(body.get("mode")).strip().lower() or default_mode
    if mode not in {"query", "queue", "execute"}:
        state.record_audit({
            "tool": tool,
            "mode": request.method,
            "client": request.remote,
            "decision": "rejected",
            "error": "mode must be query, queue, or execute",
            "path": request.path,
        })
        return web.json_response({
            "status": "error",
            "decision": "rejected",
            "error": "mode must be query, queue, or execute",
        }, status=400)

    tool_execution_mode = tool_meta.get("execution_mode")
    if mode == "query" and tool_execution_mode != "query":
        state.record_audit({
            "tool": tool,
            "mode": request.method,
            "client": request.remote,
            "decision": "rejected",
            "error": f"tool {tool} does not support query mode",
            "path": request.path,
        })
        return web.json_response({
            "status": "error",
            "decision": "rejected",
            "error": f"tool {tool} does not support query mode",
            }, status=400)
    if mode == "queue" and tool_execution_mode != "queue":
        state.record_audit({
            "tool": tool,
            "mode": request.method,
            "client": request.remote,
            "decision": "rejected",
            "error": f"tool {tool} does not support queue mode",
            "path": request.path,
        })
        return web.json_response({
            "status": "error",
            "decision": "rejected",
            "error": f"tool {tool} does not support queue mode",
            }, status=400)

    idempotency_key = _to_str(request.headers.get("Idempotency-Key") or body.get("idempotency_key"))
    if idempotency_key:
        replay = state.get_idempotent(idempotency_key)
        if replay is not None:
            return web.json_response(replay)

    client_ip = request.remote or "unknown"
    try:
        if tool == "ableton_schedule_plan":
            decision, data = await _execute_or_queue_plan(state, params, client_ip)
        elif mode == "query":
            decision, data = "accepted", await _execute_tool(state, tool, params, client_ip)
        else:
            decision, data = "accepted", await _execute_tool(state, tool, params, client_ip)
    except ConnectionError as exc:
        state.record_audit({
            "tool": tool,
            "decision": "rejected",
            "error": str(exc),
            "client": client_ip,
        })
        return web.json_response({
            "status": "error",
            "decision": "rejected",
            "error": str(exc),
        }, status=503)
    except ValueError as exc:
        if str(exc) == "requires_confirmation":
            response = {
                "status": "ok",
                "decision": "requires_confirmation",
                "tool": tool,
                "data": {},
                "timestamp": int(time.time()),
            }
            if idempotency_key:
                state.set_idempotent(idempotency_key, response)
            return web.json_response(response, status=202)
        state.record_audit({
            "tool": tool,
            "decision": "rejected",
            "error": str(exc),
            "client": client_ip,
        })
        return web.json_response({
            "status": "error",
            "decision": "rejected",
            "error": str(exc),
        }, status=400)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("command failed")
        state.record_audit({
            "tool": tool,
            "decision": "rejected",
            "error": str(exc),
            "client": client_ip,
        })
        return web.json_response({
            "status": "error",
            "decision": "rejected",
            "error": str(exc),
        }, status=500)

    state.record_audit({
        "tool": tool,
        "mode": mode,
        "decision": decision,
        "client": client_ip,
    })

    response = {
        "status": "ok",
        "decision": decision,
        "tool": tool,
        "data": data,
        "timestamp": int(time.time()),
    }

    if idempotency_key:
        state.set_idempotent(idempotency_key, response)
    return web.json_response(response)


async def http_state(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    ok, err = _payload_token(request, state,
                             request.app["config"].get("allowed_clients"))
    if not ok:
        return err
    return web.json_response({"connected": state.connected, "state": state.last_state})


async def http_status(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    ok, err = _payload_token(request, state,
                             request.app["config"].get("allowed_clients"))
    if not ok:
        return err
    return web.json_response({
        "status": "ok",
        "ableton_connected": state.connected,
        "ws_host": request.app["config"]["ws_host"],
        "ws_port": request.app["config"]["ws_port"],
    })


async def http_tools_manifest(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    ok, err = _payload_token(request, state,
                             request.app["config"].get("allowed_clients"))
    if not ok:
        return err
    manifest = dict(TOOL_MANIFEST)
    manifest["command_ids"] = sorted([v.get("id") for v in manifest["tools"].values() if isinstance(v, dict)])
    manifest["generated_at"] = int(time.time())
    return web.json_response(manifest)


async def http_audit(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    ok, err = _payload_token(request, state,
                             request.app["config"].get("allowed_clients"))
    if not ok:
        return err
    entries = state.read_audit()
    return web.json_response({"status": "ok", "count": len(entries), "entries": entries})


def build_http_app(state: BridgeState, config: Dict[str, Any]) -> web.Application:
    app = web.Application()
    app["state"] = state
    app["config"] = config
    app.router.add_post("/command", http_command)
    app.router.add_get("/state", http_state)
    app.router.add_get("/status", http_status)
    app.router.add_get("/tools/ableton/manifest", http_tools_manifest)
    app.router.add_get("/audit", http_audit)
    return app


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path and Path(path).exists():
        if yaml is None:
            raise RuntimeError("PyYAML not installed; cannot read YAML config")
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        cfg.update(user)
    import os
    if os.environ.get("BRIDGE_AUTH_TOKEN"):
        cfg["auth_token"] = os.environ["BRIDGE_AUTH_TOKEN"]
    if os.environ.get("BRIDGE_WS_PORT"):
        cfg["ws_port"] = int(os.environ["BRIDGE_WS_PORT"])
    if os.environ.get("BRIDGE_HTTP_PORT"):
        cfg["http_port"] = int(os.environ["BRIDGE_HTTP_PORT"])
    if os.environ.get("BRIDGE_HTTP_HOST"):
        cfg["http_host"] = os.environ["BRIDGE_HTTP_HOST"]
    if os.environ.get("BRIDGE_COMMAND_TIMEOUT"):
        cfg["command_timeout"] = float(os.environ["BRIDGE_COMMAND_TIMEOUT"])
    if os.environ.get("BRIDGE_IDEMPOTENCY_TTL_SECONDS"):
        cfg["idempotency_ttl_seconds"] = int(os.environ["BRIDGE_IDEMPOTENCY_TTL_SECONDS"])
    if os.environ.get("BRIDGE_IDEMPOTENCY_CACHE_SIZE"):
        cfg["idempotency_cache_size"] = int(os.environ["BRIDGE_IDEMPOTENCY_CACHE_SIZE"])
    allowed_clients = os.environ.get("BRIDGE_ALLOWED_CLIENTS")
    if allowed_clients is not None:
        parsed = [p.strip() for p in allowed_clients.split(",") if p.strip()]
        cfg["allowed_clients"] = parsed
    return cfg


def make_ssl_context(cfg: Dict[str, Any]) -> Optional[ssl.SSLContext]:
    sslcfg = cfg.get("ssl", {})
    if not sslcfg.get("enabled"):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(sslcfg["cert"], sslcfg["key"])
    return ctx


async def start_servers(cfg: Dict[str, Any]):
    state = BridgeState(
        token=cfg["auth_token"],
        command_timeout=cfg.get("command_timeout", DEFAULT_CONFIG["command_timeout"]),
        idempotency_ttl_seconds=cfg.get("idempotency_ttl_seconds",
                                       DEFAULT_CONFIG["idempotency_ttl_seconds"]),
        idempotency_cache_size=cfg.get("idempotency_cache_size",
                                      DEFAULT_CONFIG["idempotency_cache_size"]),
    )
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
    LOG.info("WebSocket server listening on %s:%d (ssl=%s)", cfg["ws_host"], cfg["ws_port"], bool(ssl_ctx))

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

    return {
        "ws_server": ws_srv,
        "runner": runner,
        "state": state,
        "config": cfg,
        "stop": _stop,
    }


async def stop_servers(control: Dict[str, Any]):
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
    _require_token(cfg)
    _ensure_loopback_host(cfg)

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
