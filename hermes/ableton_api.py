"""
Hermes-Ableton Bridge — Python API for the Hermes agent
=======================================================

Clean synchronous API the Hermes agent calls to control Ableton Live remotely.
All methods talk to the bridge's HTTP API on the VPS (default localhost:8081),
which forwards commands over the WebSocket to the Max for Live device in Ableton.

Example
-------
    from hermes.ableton_api import AbletonClient

    client = AbletonClient(host="localhost", port=8081, token="secret")
    client.play()
    client.set_tempo(120)
    client.create_midi_track()
    client.create_midi_clip(track=0, length_beats=4)
    client.add_note(track=0, clip=0, pitch=60, start=0.0, duration=0.5, velocity=100)
    state = client.get_state()

The token here is the SAME token configured in server/config.yaml and inside the
Max for Live device — it is used for the HTTP -> WS forwarding path. The actual
WebSocket auth (Ableton -> server) uses the same shared secret.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple


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
        Shared secret (must match server config and the M4L device). Currently
        kept for future HTTP auth; forwarded as a header.
    timeout : float
        Per-request HTTP timeout in seconds.
    """

    def __init__(self, host: str = "localhost", port: int = 8081,
                 token: str = "secret", timeout: float = 15.0):
        self.base_url = f"http://{host}:{port}"
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    #  Low-level HTTP
    # ------------------------------------------------------------------ #
    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Bridge-Token": self.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {"error": str(e)}
            msg = body.get("error", str(e))
            if e.code == 503:
                raise AbletonNotConnectedError(msg)
            raise AbletonError(msg)
        except urllib.error.URLError as e:
            raise AbletonError(f"cannot reach bridge at {url}: {e.reason}")

    def _get(self, path: str) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url, method="GET", headers={"X-Bridge-Token": self.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {"error": str(e)}
            raise AbletonError(body.get("error", str(e)))
        except urllib.error.URLError as e:
            raise AbletonError(f"cannot reach bridge at {url}: {e.reason}")

    def _command(self, action: str, **params: Any) -> Dict[str, Any]:
        """Send a command and return the response `data` (or raise on error)."""
        resp = self._post("/command", {"action": action, "params": params})
        if resp.get("status") != "ok":
            raise AbletonError(resp.get("error") or "unknown error")
        return resp.get("data", {}) or {}

    # ------------------------------------------------------------------ #
    #  Connection / status
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        return self._get("/status")

    def is_connected(self) -> bool:
        return bool(self.status().get("ableton_connected"))

    # ------------------------------------------------------------------ #
    #  Transport
    # ------------------------------------------------------------------ #
    def play(self) -> Dict[str, Any]:
        return self._command("play")

    def stop(self) -> Dict[str, Any]:
        return self._command("stop")

    def set_tempo(self, bpm: float) -> Dict[str, Any]:
        if not 20 <= bpm <= 999:
            raise ValueError("tempo must be between 20 and 999 BPM")
        return self._command("set_tempo", tempo=float(bpm))

    def set_time_signature(self, numerator: int, denominator: int) -> Dict[str, Any]:
        if denominator not in (1, 2, 4, 8, 16):
            raise ValueError("denominator must be 1,2,4,8 or 16")
        return self._command("set_time_signature",
                              numerator=int(numerator), denominator=int(denominator))

    def toggle_loop(self) -> Dict[str, Any]:
        return self._command("toggle_loop")

    def toggle_metronome(self) -> Dict[str, Any]:
        return self._command("toggle_metronome")

    def overdub(self, on: Optional[bool] = None) -> Dict[str, Any]:
        params = {}
        if on is not None:
            params["on"] = bool(on)
        return self._command("overdub", **params)

    # ------------------------------------------------------------------ #
    #  Tracks
    # ------------------------------------------------------------------ #
    def create_midi_track(self, index: Optional[int] = None) -> Dict[str, Any]:
        params = {} if index is None else {"index": int(index)}
        return self._command("create_midi_track", **params)

    def create_audio_track(self, index: Optional[int] = None) -> Dict[str, Any]:
        params = {} if index is None else {"index": int(index)}
        return self._command("create_audio_track", **params)

    def delete_track(self, index: int) -> Dict[str, Any]:
        return self._command("delete_track", index=int(index))

    def duplicate_track(self, index: int) -> Dict[str, Any]:
        return self._command("duplicate_track", index=int(index))

    def mute_track(self, index: int, mute: bool = True) -> Dict[str, Any]:
        return self._command("mute_track", index=int(index), mute=bool(mute))

    def solo_track(self, index: int, solo: bool = True) -> Dict[str, Any]:
        return self._command("solo_track", index=int(index), solo=bool(solo))

    def set_volume(self, index: int, volume: float) -> Dict[str, Any]:
        if not -60 <= volume <= 6:
            raise ValueError("volume in dB must be between -60 and +6")
        return self._command("set_volume", index=int(index), volume=float(volume))

    def set_pan(self, index: int, pan: float) -> Dict[str, Any]:
        if not -1 <= pan <= 1:
            raise ValueError("pan must be between -1 (left) and 1 (right)")
        return self._command("set_pan", index=int(index), pan=float(pan))

    def set_send(self, index: int, send: int, value: float) -> Dict[str, Any]:
        if not 0 <= value <= 1:
            raise ValueError("send value must be between 0 and 1")
        return self._command("set_send", index=int(index), send=int(send), value=float(value))

    # ------------------------------------------------------------------ #
    #  Clips
    # ------------------------------------------------------------------ #
    def create_midi_clip(self, track: int, length_beats: float = 4.0,
                         scene: Optional[int] = None) -> Dict[str, Any]:
        params = {"track": int(track), "length_beats": float(length_beats)}
        if scene is not None:
            params["scene"] = int(scene)
        return self._command("create_midi_clip", **params)

    def set_clip_length(self, track: int, clip: int, length_beats: float) -> Dict[str, Any]:
        return self._command("set_clip_length", track=int(track), clip=int(clip),
                             length_beats=float(length_beats))

    def add_note(self, track: int, clip: int, pitch: int, start: float,
                 duration: float, velocity: int = 100) -> Dict[str, Any]:
        if not 0 <= pitch <= 127:
            raise ValueError("pitch must be 0-127")
        if not 0 <= velocity <= 127:
            raise ValueError("velocity must be 0-127")
        return self._command("add_note", track=int(track), clip=int(clip),
                             pitch=int(pitch), start=float(start),
                             duration=float(duration), velocity=int(velocity))

    def add_notes(self, track: int, clip: int,
                  notes: Sequence[Tuple[int, float, float, int]]) -> Dict[str, Any]:
        """Add many notes at once. Each note is (pitch, start, duration, velocity)."""
        clean = []
        for n in notes:
            if len(n) < 4:
                raise ValueError("each note must be (pitch, start, duration, velocity)")
            pitch, start, duration, velocity = n[0], n[1], n[2], n[3]
            if not 0 <= pitch <= 127:
                raise ValueError(f"pitch {pitch} out of range 0-127")
            if not 0 <= velocity <= 127:
                raise ValueError(f"velocity {velocity} out of range 0-127")
            clean.append({"pitch": int(pitch), "start": float(start),
                          "duration": float(duration), "velocity": int(velocity)})
        return self._command("add_notes", track=int(track), clip=int(clip), notes=clean)

    def remove_note(self, track: int, clip: int, pitch: int, start: float) -> Dict[str, Any]:
        return self._command("remove_note", track=int(track), clip=int(clip),
                             pitch=int(pitch), start=float(start))

    def clear_clip(self, track: int, clip: int) -> Dict[str, Any]:
        return self._command("clear_clip", track=int(track), clip=int(clip))

    def quantize_clip(self, track: int, clip: int, grid: int = 4) -> Dict[str, Any]:
        """grid is notes-per-beat (4 = 1/16)."""
        return self._command("quantize_clip", track=int(track), clip=int(clip), grid=int(grid))

    def toggle_clip_loop(self, track: int, clip: int) -> Dict[str, Any]:
        return self._command("toggle_clip_loop", track=int(track), clip=int(clip))

    # ------------------------------------------------------------------ #
    #  Browser
    # ------------------------------------------------------------------ #
    def load_instrument(self, track: int, name: str) -> Dict[str, Any]:
        return self._command("load_instrument", track=int(track), name=str(name))

    def load_effect(self, track: int, name: str) -> Dict[str, Any]:
        return self._command("load_effect", track=int(track), name=str(name))

    def load_sample(self, track: int, name: str) -> Dict[str, Any]:
        return self._command("load_sample", track=int(track), name=str(name))

    def load_drum_rack(self, track: int, name: str = "") -> Dict[str, Any]:
        return self._command("load_drum_rack", track=int(track), name=str(name))

    # ------------------------------------------------------------------ #
    #  Devices
    # ------------------------------------------------------------------ #
    def set_device_parameter(self, track: int, device: int, param: int,
                              value: float) -> Dict[str, Any]:
        if not 0 <= value <= 1:
            raise ValueError("device parameter value must be 0-1")
        return self._command("set_device_parameter", track=int(track), device=int(device),
                             param=int(param), value=float(value))

    def get_device_parameters(self, track: int, device: int) -> Dict[str, Any]:
        return self._command("get_device_parameters", track=int(track), device=int(device))

    # ------------------------------------------------------------------ #
    #  Scenes
    # ------------------------------------------------------------------ #
    def create_scene(self, name: str = "") -> Dict[str, Any]:
        return self._command("create_scene", name=str(name))

    def launch_scene(self, scene: int) -> Dict[str, Any]:
        return self._command("launch_scene", scene=int(scene))

    def reorder_scene(self, scene: int, new_index: int) -> Dict[str, Any]:
        return self._command("reorder_scene", scene=int(scene), new_index=int(new_index))

    # ------------------------------------------------------------------ #
    #  State
    # ------------------------------------------------------------------ #
    def get_state(self) -> Dict[str, Any]:
        """Return last known state cached on the bridge (no round-trip to Ableton)."""
        return self._get("/state")

    def get_full_state(self) -> Dict[str, Any]:
        """Ask Ableton to report its full current state (round-trip command)."""
        return self._command("get_full_state")

    def request_state_refresh(self) -> Dict[str, Any]:
        """Trigger a fresh state report from Ableton."""
        return self.get_full_state()


# Make the module runnable as a quick CLI smoke test:
#   python -m hermes.ableton_api play
if __name__ == "__main__":  # pragma: no cover
    import sys
    c = AbletonClient()
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    method = getattr(c, action, None)
    if method is None:
        print(f"unknown action: {action}")
        sys.exit(1)
    import pprint
    pprint.pprint(method())
