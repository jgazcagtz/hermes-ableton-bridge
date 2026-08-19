"""
Hermes-Ableton Bridge — Python API for the Hermes agent
=======================================================

Curated v1 HTTP client that talks to the VPS server through the explicit
`/command` tool schema.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class AbletonError(Exception):
    """Raised when the bridge or Ableton reports an error."""


class AbletonNotConnectedError(AbletonError):
    """Raised when no Ableton client is connected to the bridge."""


class AbletonClient:
    """Synchronous client for the Hermes-Ableton bridge HTTP API.

    Parameters
    ----------
    host : str
        Host where the bridge server runs (the VPS, usually "localhost").
    port : int
        HTTP API port (default 8081).
    token : str
        Shared secret that must match server config and the bridge token.
    timeout : float
        Per-request HTTP timeout in seconds.
    """

    def __init__(self, host: str = "localhost", port: int = 8081,
                 token: str = "secret", timeout: float = 15.0):
        self.base_url = f"http://{host}:{port}"
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------------
    #  Low-level HTTP
    # ------------------------------------------------------------------
    def _post(self, path: str, payload: Dict[str, Any],
              idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Bridge-Token": self.token,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # pragma: no cover - exercised by integration tests
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {"error": str(e)}
            msg = body.get("error", str(e))
            if e.code == 503:
                raise AbletonNotConnectedError(msg)
            if e.code == 401:
                raise AbletonError(f"bridge auth failed: {msg}")
            raise AbletonError(msg)
        except urllib.error.URLError as e:  # pragma: no cover
            raise AbletonError(f"cannot reach bridge at {url}: {e.reason}")

    def _get(self, path: str) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"X-Bridge-Token": self.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {"error": str(e)}
            raise AbletonError(body.get("error", str(e)))
        except urllib.error.URLError as e:
            raise AbletonError(f"cannot reach bridge at {url}: {e.reason}")

    def _command(self, tool: str, mode: Optional[str] = "execute",
                 idempotency_key: Optional[str] = None,
                 **params: Any) -> Dict[str, Any]:
        payload = {"tool": tool, "params": params}
        if mode is not None:
            payload["mode"] = mode
        resp = self._post("/command", payload, idempotency_key=idempotency_key)
        if resp.get("status") != "ok":
            raise AbletonError(resp.get("error") or "unknown error")
        return resp.get("data", {}) or {}

    # ------------------------------------------------------------------
    #  Utility
    # ------------------------------------------------------------------
    def _validate_range(self, name: str, value: float, min_value: float,
                       max_value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        if value < min_value or value > max_value:
            raise ValueError(f"{name} must be between {min_value} and {max_value}")
        return float(value)

    def _validate_note_list(self, notes: Sequence[Any]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for index, note in enumerate(notes):
            if isinstance(note, dict):
                pitch = note.get("pitch")
                start = note.get("start")
                duration = note.get("duration")
                velocity = note.get("velocity", 100)
            else:
                if len(note) < 4:
                    raise ValueError(
                        f"note[{index}] must be (pitch,start,duration,velocity)"
                    )
                pitch, start, duration, velocity = note[0], note[1], note[2], note[3]

            if not isinstance(pitch, (int, float)) or not 0 <= int(pitch) <= 127:
                raise ValueError(f"note[{index}].pitch must be 0..127")
            if not isinstance(velocity, (int, float)) or not 0 <= int(velocity) <= 127:
                raise ValueError(f"note[{index}].velocity must be 0..127")
            if not isinstance(start, (int, float)) or start < 0:
                raise ValueError(f"note[{index}].start must be >= 0")
            if not isinstance(duration, (int, float)) or duration < 0:
                raise ValueError(f"note[{index}].duration must be >= 0")

            normalized.append({
                "pitch": int(pitch),
                "start": float(start),
                "duration": float(duration),
                "velocity": int(velocity),
            })
        return normalized

    # ------------------------------------------------------------------
    #  Connection / status
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return self._get("/status")

    def is_connected(self) -> bool:
        return bool(self.status().get("ableton_connected"))

    # ------------------------------------------------------------------
    #  Transport
    # ------------------------------------------------------------------
    def transport(self, action: str, on: Optional[bool] = None) -> Dict[str, Any]:
        action = str(action).strip().lower()
        if action not in {"play", "stop", "loop", "record"}:
            raise ValueError("action must be play|stop|loop|record")
        payload: Dict[str, Any] = {"action": action}
        if action == "loop" and on is not None:
            payload["on"] = bool(on)
        if action == "record":
            if on is None:
                raise ValueError("record requires on=True|False")
            payload["on"] = bool(on)
        return self._command("ableton_transport", **payload)

    def play(self) -> Dict[str, Any]:
        return self.transport("play")

    def stop(self) -> Dict[str, Any]:
        return self.transport("stop")

    def toggle_loop(self, on: bool) -> Dict[str, Any]:
        return self.transport("loop", on=bool(on))

    def record(self, on: bool) -> Dict[str, Any]:
        return self.transport("record", on=bool(on))

    def set_tempo(self, bpm: float) -> Dict[str, Any]:
        bpm_v = self._validate_range("tempo", bpm, 20.0, 999.0)
        return self._command("ableton_set_tempo", tempo=bpm_v)

    def get_timing(self) -> Dict[str, Any]:
        return self._command("ableton_get_timing", mode="query")

    # ------------------------------------------------------------------
    #  Tracks
    # ------------------------------------------------------------------
    def create_track(self, track_type: str, index: Optional[int] = None) -> Dict[str, Any]:
        t = str(track_type).strip().lower()
        if t not in {"midi", "audio"}:
            raise ValueError("track_type must be 'midi' or 'audio'")
        payload = {"track_type": t}
        if index is not None:
            payload["index"] = int(index)
        return self._command("ableton_create_track", **payload)

    def create_midi_track(self, index: Optional[int] = None) -> Dict[str, Any]:
        return self.create_track("midi", index=index)

    def create_audio_track(self, index: Optional[int] = None) -> Dict[str, Any]:
        return self.create_track("audio", index=index)

    def set_track_level(
        self,
        track: int,
        *,
        volume: Optional[float] = None,
        pan: Optional[float] = None,
        mute: Optional[bool] = None,
        solo: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"track": int(track)}
        if volume is not None:
            payload["volume"] = self._validate_range("volume", float(volume), -60.0, 6.0)
        if pan is not None:
            payload["pan"] = self._validate_range("pan", float(pan), -1.0, 1.0)
        if mute is not None:
            payload["mute"] = bool(mute)
        if solo is not None:
            payload["solo"] = bool(solo)
        if not payload.keys() - {"track"}:
            raise ValueError(
                "set_track_level requires at least one of volume, pan, mute, solo"
            )
        return self._command("ableton_set_track_level", **payload)

    def mute_track(self, index: int, mute: bool = True) -> Dict[str, Any]:
        return self.set_track_level(index, mute=bool(mute))

    def solo_track(self, index: int, solo: bool = True) -> Dict[str, Any]:
        return self.set_track_level(index, solo=bool(solo))

    def set_volume(self, index: int, volume: float) -> Dict[str, Any]:
        return self.set_track_level(index, volume=float(volume))

    def set_pan(self, index: int, pan: float) -> Dict[str, Any]:
        return self.set_track_level(index, pan=float(pan))

    # ------------------------------------------------------------------
    #  Clips
    # ------------------------------------------------------------------
    def create_midi_clip(self, track: int, length_beats: float = 4.0,
                         scene: Optional[int] = None) -> Dict[str, Any]:
        payload = {
            "track": int(track),
            "length_beats": self._validate_range("length_beats", float(length_beats), 0.001,
                                                  10_000.0),
        }
        if scene is not None:
            payload["scene"] = int(scene)
        return self._command("ableton_create_clip", **payload)

    def replace_clip_notes(
        self,
        track: int,
        clip: int,
        notes: Sequence[Any],
        *,
        confirm: bool = True,
    ) -> Dict[str, Any]:
        payload = {
            "track": int(track),
            "clip": int(clip),
            "notes": self._validate_note_list(notes),
            "confirm": bool(confirm),
        }
        return self._command("ableton_replace_clip_notes", **payload)

    def remove_clip_note(
        self,
        track: int,
        clip: int,
        pitch: int,
        start: float,
        *,
        confirm: bool = True,
    ) -> Dict[str, Any]:
        pitch_val = int(self._validate_range("pitch", float(pitch), 0.0, 127.0))
        if float(pitch_val) != float(pitch):
            raise ValueError("pitch must be an integer")
        payload = {
            "track": int(track),
            "clip": int(clip),
            "pitch": pitch_val,
            "start": self._validate_range("start", float(start), 0.0, 10_000.0),
            "confirm": bool(confirm),
        }
        return self._command("ableton_remove_clip_note", **payload)

    def set_clip_loop(self, track: int, clip: int, loop: bool) -> Dict[str, Any]:
        return self._command("ableton_set_clip_loop", track=int(track), clip=int(clip), loop=bool(loop))

    def toggle_clip_loop(self, track: int, clip: int) -> Dict[str, Any]:
        return self._command("ableton_toggle_clip_loop", track=int(track), clip=int(clip))

    def launch_clip(self, track: int, clip: int) -> Dict[str, Any]:
        return self._command("ableton_launch_clip", track=int(track), clip=int(clip))

    def clear_clip(self, track: int, clip: int, confirm: bool = True) -> Dict[str, Any]:
        return self._command("ableton_clear_clip", track=int(track), clip=int(clip),
                             confirm=bool(confirm))

    def stop_clip(self, track: int, clip: int) -> Dict[str, Any]:
        return self._command("ableton_stop_clip", track=int(track), clip=int(clip))

    # ------------------------------------------------------------------
    #  Scenes
    # ------------------------------------------------------------------
    def launch_scene(self, scene: int) -> Dict[str, Any]:
        return self._command("ableton_launch_scene", scene=int(scene))

    # ------------------------------------------------------------------
    #  Scheduling / performance flow
    # ------------------------------------------------------------------
    def schedule_plan(
        self,
        tracks: Iterable[Dict[str, Any]],
        *,
        alignment: str = "next_bar",
        preview: bool = False,
        confirm: bool = False,
        plan_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_tracks: List[Dict[str, Any]] = []
        for track_payload in tracks:
            if not isinstance(track_payload, dict):
                raise ValueError("tracks must be a list of objects")
            normalized_tracks.append({
                "track": int(track_payload["track"]),
                "clip": int(track_payload["clip"]),
                "notes": self._validate_note_list(track_payload.get("notes", [])),
            })
        payload = {
            "alignment": str(alignment).strip().lower(),
            "tracks": normalized_tracks,
            "preview": bool(preview),
            "confirm": bool(confirm),
        }
        if plan_id is not None:
            payload["plan_id"] = str(plan_id)
        return self._command("ableton_schedule_plan", mode="queue", **payload,
                             idempotency_key=idempotency_key)

    # ------------------------------------------------------------------
    #  State
    # ------------------------------------------------------------------
    def get_state(self) -> Dict[str, Any]:
        return self._get("/state")

    def ableton_status(self, *, fresh: bool = False) -> Dict[str, Any]:
        return self._command("ableton_status", mode="query", fresh=bool(fresh))

    def get_full_state(self) -> Dict[str, Any]:
        return self.ableton_status(fresh=True)

    # ------------------------------------------------------------------
    #  Legacy methods intentionally removed from v1 toolset
    # ------------------------------------------------------------------
    def delete_track(self, *_, **__):
        raise AbletonError("delete_track is not in v1 toolset. Use deterministic schedule or explicit policy.")

    def duplicate_track(self, *_, **__):
        raise AbletonError("duplicate_track is not in v1 toolset.")

    def toggle_metronome(self, *_, **__):
        raise AbletonError("toggle_metronome is not in v1 toolset.")

    def add_note(self, *_, **__):
        raise AbletonError("add_note is retired. Use replace_clip_notes(notes=[...]).")

    def add_notes(self, *_, **__):
        raise AbletonError("add_notes is retired. Use replace_clip_notes(notes=[...]).")

    def quantize_clip(self, *_, **__):
        raise AbletonError("quantize_clip is not in v1 toolset.")

    def set_send(self, *_, **__):
        raise AbletonError("set_send is not in v1 toolset.")

    def set_device_parameter(self, *_, **__):
        raise AbletonError("set_device_parameter is not in v1 toolset.")

    def get_device_parameters(self, *_, **__):
        raise AbletonError("get_device_parameters is not in v1 toolset.")

    def create_scene(self, *_, **__):
        raise AbletonError("create_scene is not in v1 toolset.")

    def reorder_scene(self, *_, **__):
        raise AbletonError("reorder_scene is not in v1 toolset.")

    def load_instrument(self, *_, **__):
        raise AbletonError("load_instrument is not in v1 toolset.")

    def load_effect(self, *_, **__):
        raise AbletonError("load_effect is not in v1 toolset.")

    def load_sample(self, *_, **__):
        raise AbletonError("load_sample is not in v1 toolset.")

    def load_drum_rack(self, *_, **__):
        raise AbletonError("load_drum_rack is not in v1 toolset.")


if __name__ == "__main__":
    import sys
    import pprint

    c = AbletonClient()
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    method = getattr(c, action, None)
    if method is None:
        print(f"unknown action: {action}")
        sys.exit(1)
    if action == "replace_clip_notes":
        # basic smoke test entry
        # Usage: python -m hermes.ableton_api replace_clip_notes <track> <clip>
        track = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        clip = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        result = method(track, clip, [])
        pprint.pprint(result)
    else:
        result = method()
        pprint.pprint(result)
