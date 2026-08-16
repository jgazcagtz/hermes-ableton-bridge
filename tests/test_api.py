"""Hermes-Ableton bridge — unit tests for the Hermes commands API.

Boots a real ws_server + mock Max-for-Live client in-process, then exercises the
AbletonClient Python API against them. Also tests chord_helpers.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server import ws_server  # noqa: E402
from tests.mock_client import run as mock_run  # noqa: E402
from hermes.ableton_api import AbletonClient, AbletonError  # noqa: E402
from hermes import chord_helpers  # noqa: E402

TOKEN = "test-token-123"
WS_PORT = 8200
HTTP_PORT = 8201


async def _boot():
    cfg = {
        "ws_host": "127.0.0.1", "ws_port": WS_PORT,
        "http_host": "127.0.0.1", "http_port": HTTP_PORT,
        "auth_token": TOKEN, "command_timeout": 5.0,
        "state_interval": 0.3, "log_level": "WARNING",
        "ssl": {"enabled": False},
    }
    control = await ws_server.start_servers(cfg)
    mock_task = asyncio.create_task(mock_run("127.0.0.1", WS_PORT, TOKEN, 0.4, None))
    # wait for connection (async polling — don't block the loop)
    import aiohttp
    deadline = time.time() + 8
    async with aiohttp.ClientSession() as session:
        while time.time() < deadline:
            try:
                async with session.get(f"http://127.0.0.1:{HTTP_PORT}/status",
                                       timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if (await r.json()).get("ableton_connected"):
                        break
            except Exception:
                pass
            await asyncio.sleep(0.15)
    return control, mock_task


def _call(client, method, *a, **k):
    """Run a synchronous AbletonClient call off the event loop."""
    return asyncio.to_thread(lambda: getattr(client, method)(*a, **k))


@pytest.fixture
def client():
    return AbletonClient(host="127.0.0.1", port=HTTP_PORT, token=TOKEN, timeout=6)


# --------------------------------------------------------------------------- #
#  Live integration tests (server + mock)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_transport_commands(client):
    control, mock_task = await _boot()
    try:
        assert await _call(client, "is_connected")
        await _call(client, "play"); assert True
        assert (await _call(client, "set_tempo", 128))["tempo"] == 128
        assert (await _call(client, "stop")).get("playing") is False
        assert (await _call(client, "toggle_metronome")).get("metronome") is True
    finally:
        mock_task.cancel()
        await control["stop"]()


@pytest.mark.asyncio
async def test_track_management(client):
    control, mock_task = await _boot()
    try:
        r = await _call(client, "create_midi_track", 1)
        assert "track" in r
        await _call(client, "set_volume", 1, -3.0)
        await _call(client, "set_pan", 1, 0.25)
        await _call(client, "mute_track", 1, True)
        await _call(client, "solo_track", 0, False)
        st = await _call(client, "get_state")
        assert st["connected"] is True
    finally:
        mock_task.cancel()
        await control["stop"]()


@pytest.mark.asyncio
async def test_clips_and_notes(client):
    control, mock_task = await _boot()
    try:
        await _call(client, "create_midi_clip", track=0, length_beats=4.0)
        await _call(client, "add_note", track=0, clip=0, pitch=60, start=0.0, duration=0.5, velocity=100)
        await _call(client, "add_notes", track=0, clip=0, notes=[(64, 0.5, 0.5, 90), (67, 1.0, 0.5, 80)])
        st = await _call(client, "get_full_state")
        track0 = st["tracks"][0]
        assert len(track0["clips"]) >= 1
        assert len(track0["clips"][0]["notes"]) >= 3
    finally:
        mock_task.cancel()
        await control["stop"]()


@pytest.mark.asyncio
async def test_devices_and_browser(client):
    control, mock_task = await _boot()
    try:
        await _call(client, "load_instrument", track=0, name="Serum")
        await _call(client, "load_effect", track=0, name="Reverb")
        params = await _call(client, "get_device_parameters", track=0, device=0)
        assert isinstance(params.get("parameters"), list)
        await _call(client, "set_device_parameter", track=0, device=0, param=0, value=0.7)
    finally:
        mock_task.cancel()
        await control["stop"]()


@pytest.mark.asyncio
async def test_scenes(client):
    control, mock_task = await _boot()
    try:
        await _call(client, "create_scene", "Intro")
        await _call(client, "launch_scene", 0)
        await _call(client, "reorder_scene", 0, 1)
    finally:
        mock_task.cancel()
        await control["stop"]()


@pytest.mark.asyncio
async def test_command_without_connection_returns_error():
    cfg = {
        "ws_host": "127.0.0.1", "ws_port": 8210,
        "http_host": "127.0.0.1", "http_port": 8211,
        "auth_token": TOKEN, "command_timeout": 3.0,
        "state_interval": 0.3, "log_level": "WARNING",
        "ssl": {"enabled": False},
    }
    control = await ws_server.start_servers(cfg)
    try:
        c = AbletonClient(host="127.0.0.1", port=8211, token=TOKEN, timeout=4)
        with pytest.raises(AbletonError):
            await _call(c, "play")
    finally:
        await control["stop"]()


# --------------------------------------------------------------------------- #
#  Validation tests (no server needed)
# --------------------------------------------------------------------------- #
def test_invalid_tempo():
    c = AbletonClient(host="127.0.0.1", port=1, token="x")
    with pytest.raises(ValueError):
        c.set_tempo(1)
    with pytest.raises(ValueError):
        c.set_tempo(2000)


def test_invalid_note_pitch():
    c = AbletonClient(host="127.0.0.1", port=1, token="x")
    with pytest.raises(ValueError):
        c.add_note(track=0, clip=0, pitch=200, start=0, duration=1, velocity=100)
    with pytest.raises(ValueError):
        c.add_notes(track=0, clip=0, notes=[(60, 0, 0.5, 200)])


# --------------------------------------------------------------------------- #
#  chord_helpers tests (mock client)
# --------------------------------------------------------------------------- #
class RecordingClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def fn(*a, **k):
            self.calls.append((name, a, k))
            return {"ok": True}
        return fn


def test_create_chord_progression():
    c = RecordingClient()
    res = chord_helpers.create_chord_progression(c, track=0, key="C",
                                                 progression_type="I-V-vi-IV", bpm=100)
    assert res["chords"] == 4
    assert res["clip_length_beats"] == 16
    actions = [x[0] for x in c.calls]
    assert "set_tempo" in actions
    assert "create_midi_clip" in actions
    assert "add_notes" in actions


def test_create_drum_pattern():
    c = RecordingClient()
    res = chord_helpers.create_drum_pattern(c, track=0, pattern_name="trap", bpm=140, bars=2)
    assert res["pattern"] == "trap"
    assert res["bars"] == 2
    assert res["notes"] > 0


def test_create_scale_melody():
    c = RecordingClient()
    res = chord_helpers.create_scale_melody(c, track=0, key="A", scale="minor",
                                            num_notes=8, seed=42)
    assert res["num_notes"] == 8
    assert len(res["notes"]) == 8


def test_music_theory_helpers():
    assert chord_helpers.note_to_midi("C", 4) == 60
    assert chord_helpers.note_to_midi("A", 4) == 69
    assert chord_helpers.key_to_root("C#") == 49  # C# octave 3 (default low octave)
    notes = chord_helpers.scale_notes(60, "major")
    assert 60 in notes and 72 in notes
    chord = chord_helpers.build_chord(60, "maj")
    assert chord == [60, 64, 67]
