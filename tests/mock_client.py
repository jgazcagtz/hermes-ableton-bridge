#!/usr/bin/env python3
"""
Hermes-Ableton Bridge — Mock Max for Live client
=================================================

Simulates the Max for Live device so the whole bridge can be tested WITHOUT
Ableton running. It connects to the bridge's WebSocket server, authenticates,
and responds to commands with fake but realistic state.

Usage
-----
1. Start the bridge server (in another terminal):
       cd server && python ws_server.py --config config.yaml

2. Run this mock client:
       python tests/mock_client.py --host 127.0.0.1 --port 8080 --token change-me-please

   Or with defaults from server/config.yaml:
       python tests/mock_client.py

The mock maintains an in-memory model of a fake Live set (tempo, tracks, clips,
devices) so repeated get_state / get_full_state commands reflect prior changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time

import websockets

LOG = logging.getLogger("mock-client")

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class FakeAbleton:
    """An in-memory model of an Ableton Live set."""

    def __init__(self):
        self.tempo = 120.0
        self.playing = False
        self.loop_on = False
        self.metronome = False
        self.overdub_on = False
        self.time_signature = (4, 4)
        self.tracks = [
            {"index": 0, "name": "1 MIDI", "type": "midi", "volume": 0.0,
             "pan": 0.0, "mute": False, "solo": False, "sends": [0.0, 0.0],
             "clips": [], "devices": []},
        ]
        self.scenes = [{"index": 0, "name": "Scene 1"}]

    # -- helpers --
    def _track(self, index):
        for t in self.tracks:
            if t["index"] == index:
                return t
        raise IndexError(f"no track at index {index}")

    def full_state(self):
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

    # -- command dispatch --
    def execute(self, action: str, params: dict) -> dict:
        fn = getattr(self, f"cmd_{action}", None)
        if fn is None:
            raise ValueError(f"unknown action '{action}'")
        return fn(params) or {}

    # Transport
    def cmd_play(self, p): self.playing = True; return {"playing": True}
    def cmd_stop(self, p): self.playing = False; return {"playing": False}
    def cmd_set_tempo(self, p):
        self.tempo = float(p["tempo"]); return {"tempo": self.tempo}
    def cmd_set_time_signature(self, p):
        self.time_signature = (int(p["numerator"]), int(p["denominator"]))
        return {"time_signature": list(self.time_signature)}
    def cmd_toggle_loop(self, p): self.loop_on = not self.loop_on; return {"loop": self.loop_on}
    def cmd_toggle_metronome(self, p): self.metronome = not self.metronome; return {"metronome": self.metronome}
    def cmd_overdub(self, p):
        self.overdub_on = bool(p.get("on", not self.overdub_on)); return {"overdub": self.overdub_on}

    # Tracks
    def cmd_create_midi_track(self, p):
        idx = p.get("index", len(self.tracks))
        self.tracks.insert(idx, {"index": idx, "name": f"{idx+1} MIDI", "type": "midi",
                                  "volume": 0.0, "pan": 0.0, "mute": False, "solo": False,
                                  "sends": [0.0, 0.0], "clips": [], "devices": []})
        self._reindex(); return {"track": idx}
    def cmd_create_audio_track(self, p):
        idx = p.get("index", len(self.tracks))
        self.tracks.insert(idx, {"index": idx, "name": f"{idx+1} Audio", "type": "audio",
                                  "volume": 0.0, "pan": 0.0, "mute": False, "solo": False,
                                  "sends": [0.0, 0.0], "clips": [], "devices": []})
        self._reindex(); return {"track": idx}
    def cmd_delete_track(self, p):
        t = self._track(int(p["index"])); self.tracks.remove(t); self._reindex()
        return {"deleted": int(p["index"])}
    def cmd_duplicate_track(self, p):
        t = self._track(int(p["index"]))
        import copy
        new = copy.deepcopy(t); new["index"] = t["index"] + 1
        self.tracks.insert(t["index"] + 1, new); self._reindex()
        return {"track": new["index"]}
    def cmd_mute_track(self, p):
        t = self._track(int(p["index"])); t["mute"] = bool(p.get("mute", True))
        return {"track": t["index"], "mute": t["mute"]}
    def cmd_solo_track(self, p):
        t = self._track(int(p["index"])); t["solo"] = bool(p.get("solo", True))
        return {"track": t["index"], "solo": t["solo"]}
    def cmd_set_volume(self, p):
        t = self._track(int(p["index"])); t["volume"] = float(p["volume"])
        return {"track": t["index"], "volume": t["volume"]}
    def cmd_set_pan(self, p):
        t = self._track(int(p["index"])); t["pan"] = float(p["pan"])
        return {"track": t["index"], "pan": t["pan"]}
    def cmd_set_send(self, p):
        t = self._track(int(p["index"]))
        while len(t["sends"]) <= int(p["send"]):
            t["sends"].append(0.0)
        t["sends"][int(p["send"])] = float(p["value"])
        return {"track": t["index"], "send": int(p["send"]), "value": float(p["value"])}

    # Clips
    def cmd_create_midi_clip(self, p):
        t = self._track(int(p["track"]))
        scene = p.get("scene", len(t["clips"]))
        clip = {"index": len(t["clips"]), "length_beats": float(p.get("length_beats", 4.0)),
                "notes": [], "loop": True}
        t["clips"].append(clip)
        return {"track": t["index"], "clip": clip["index"], "scene": scene}
    def cmd_set_clip_length(self, p):
        t = self._track(int(p["track"])); c = t["clips"][int(p["clip"])]
        c["length_beats"] = float(p["length_beats"])
        return {"length_beats": c["length_beats"]}
    def cmd_add_note(self, p):
        t = self._track(int(p["track"])); c = t["clips"][int(p["clip"])]
        c["notes"].append({"pitch": int(p["pitch"]), "start": float(p["start"]),
                           "duration": float(p["duration"]), "velocity": int(p["velocity"])})
        return {"notes": len(c["notes"])}
    def cmd_add_notes(self, p):
        t = self._track(int(p["track"])); c = t["clips"][int(p["clip"])]
        for n in p["notes"]:
            c["notes"].append({"pitch": int(n["pitch"]), "start": float(n["start"]),
                                "duration": float(n["duration"]), "velocity": int(n["velocity"])})
        return {"notes": len(c["notes"])}
    def cmd_remove_note(self, p):
        t = self._track(int(p["track"])); c = t["clips"][int(p["clip"])]
        c["notes"] = [n for n in c["notes"]
                      if not (n["pitch"] == int(p["pitch"]) and abs(n["start"] - float(p["start"])) < 1e-6)]
        return {"notes": len(c["notes"])}
    def cmd_clear_clip(self, p):
        t = self._track(int(p["track"])); c = t["clips"][int(p["clip"])]
        c["notes"] = []; return {"cleared": True}
    def cmd_quantize_clip(self, p):
        grid = float(p.get("grid", 4))
        t = self._track(int(p["track"])); c = t["clips"][int(p["clip"])]
        step = 1.0 / grid
        for n in c["notes"]:
            n["start"] = round(n["start"] / step) * step
        return {"quantized": len(c["notes"])}
    def cmd_toggle_clip_loop(self, p):
        t = self._track(int(p["track"])); c = t["clips"][int(p["clip"])]
        c["loop"] = not c.get("loop", True); return {"loop": c["loop"]}

    # Browser
    def cmd_load_instrument(self, p):
        t = self._track(int(p["track"]))
        t["devices"].append({"name": p["name"], "type": "instrument",
                              "parameters": [0.5, 0.5, 0.5]})
        return {"loaded": p["name"], "device": len(t["devices"]) - 1}
    def cmd_load_effect(self, p):
        t = self._track(int(p["track"]))
        t["devices"].append({"name": p["name"], "type": "audio_effect",
                              "parameters": [0.5, 0.5, 0.5, 0.5]})
        return {"loaded": p["name"], "device": len(t["devices"]) - 1}
    def cmd_load_sample(self, p):
        t = self._track(int(p["track"]))
        t["devices"].append({"name": p["name"], "type": "sample"})
        return {"loaded": p["name"]}
    def cmd_load_drum_rack(self, p):
        t = self._track(int(p["track"]))
        name = p.get("name", "Drum Rack")
        t["devices"].append({"name": name, "type": "drum_rack", "parameters": [0.5]})
        return {"loaded": name}

    # Devices
    def cmd_set_device_parameter(self, p):
        t = self._track(int(p["track"])); d = t["devices"][int(p["device"])]
        while len(d["parameters"]) <= int(p["param"]):
            d["parameters"].append(0.5)
        d["parameters"][int(p["param"])] = float(p["value"])
        return {"value": d["parameters"][int(p["param"])]}
    def cmd_get_device_parameters(self, p):
        t = self._track(int(p["track"])); d = t["devices"][int(p["device"])]
        return {"parameters": d.get("parameters", [])}

    # Scenes
    def cmd_create_scene(self, p):
        idx = len(self.scenes)
        self.scenes.append({"index": idx, "name": p.get("name", f"Scene {idx+1}")})
        return {"scene": idx}
    def cmd_launch_scene(self, p):
        return {"launched": int(p["scene"])}
    def cmd_reorder_scene(self, p):
        s = self.scenes.pop(int(p["scene"]))
        self.scenes.insert(int(p["new_index"]), s); self._reindex_scenes()
        return {"reordered": True}

    # State
    def cmd_get_full_state(self, p):
        return self.full_state()

    # internal
    def _reindex(self):
        for i, t in enumerate(self.tracks):
            t["index"] = i
    def _reindex_scenes(self):
        for i, s in enumerate(self.scenes):
            s["index"] = i


async def run(host: str, port: int, token: str, state_interval: float, url: str):
    fake = FakeAbleton()
    uri = url or f"ws://{host}:{port}"
    attempt = 0
    while True:
        attempt += 1
        try:
            LOG.info("Connecting to %s ...", uri)
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"auth": token}))
                auth_resp = json.loads(await ws.recv())
                if auth_resp.get("status") != "ok":
                    LOG.error("Auth failed: %s", auth_resp)
                    return
                LOG.info("Authenticated. Listening for commands.")
                attempt = 0

                async def report_state():
                    while True:
                        await asyncio.sleep(state_interval)
                        try:
                            await ws.send(json.dumps({"type": "state", "data": fake.full_state(),
                                                      "timestamp": int(time.time())}))
                        except Exception:
                            return

                state_task = asyncio.create_task(report_state())
                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            LOG.warning("bad json: %r", raw[:200])
                            continue
                        if msg.get("type") != "command":
                            continue
                        mid = msg.get("id")
                        action = msg.get("action")
                        params = msg.get("params", {})
                        LOG.info("cmd: %s %s", action, params)
                        try:
                            data = fake.execute(action, params)
                            resp = {"id": mid, "type": "response", "status": "ok",
                                    "data": data, "error": None, "timestamp": int(time.time())}
                        except Exception as e:  # noqa: BLE001
                            resp = {"id": mid, "type": "response", "status": "error",
                                    "data": {}, "error": str(e), "timestamp": int(time.time())}
                        await ws.send(json.dumps(resp))
                finally:
                    state_task.cancel()
        except (OSError, websockets.ConnectionClosed) as e:
            delay = min(30, 2 ** attempt)
            LOG.warning("Connection lost: %s — reconnecting in %ds", e, delay)
            await asyncio.sleep(delay)


def load_defaults_from_config() -> dict:
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "server", "config.yaml")
    cfg_path = os.path.abspath(cfg_path)
    if os.path.exists(cfg_path) and yaml is not None:
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = load_defaults_from_config()
    parser = argparse.ArgumentParser(description="Mock Max-for-Live client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=cfg.get("ws_port", 8080))
    parser.add_argument("--token", default=cfg.get("auth_token", "change-me-please"))
    parser.add_argument("--url", default=None, help="Full ws:// URL (overrides host/port)")
    parser.add_argument("--state-interval", type=float, default=2.0)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.host, args.port, args.token, args.state_interval, args.url))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
