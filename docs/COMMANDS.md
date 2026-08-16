# Command Reference

All commands available through the `AbletonClient` Python API.

## Connection

### `status()`
Check if Ableton is connected to the bridge.
```python
client.status()
# → {"status": "ok", "ableton_connected": true, "ws_host": "0.0.0.0", "ws_port": 8080}
```

### `is_connected()`
Returns True/False.
```python
client.is_connected()
# → True
```

---

## Transport

### `play()`
Start playback.
```python
client.play()
# → {"playing": true}
```

### `stop()`
Stop playback.
```python
client.stop()
# → {"playing": false}
```

### `set_tempo(bpm)`
Set the tempo. Range: 20-999 BPM.
```python
client.set_tempo(124)
# → {"tempo": 124.0}
```

### `set_time_signature(numerator, denominator)`
Set time signature. Denominator must be 1, 2, 4, 8, or 16.
```python
client.set_time_signature(6, 8)
# → {"time_signature": [6, 8]}
```

### `toggle_loop()`
Toggle the loop state.
```python
client.toggle_loop()
# → {"loop": true}
```

### `toggle_metronome()`
Toggle the metronome.
```python
client.toggle_metronome()
# → {"metronome": true}
```

### `overdub(on=None)`
Toggle or set overdub state.
```python
client.overdub(True)
# → {"overdub": true}
```

---

## Tracks

### `create_midi_track(index=None)`
Create a new MIDI track. If `index` is omitted, appends at the end.
```python
client.create_midi_track()        # append
client.create_midi_track(2)        # insert at index 2
# → {"track": 2, "count": 3}
```

### `create_audio_track(index=None)`
Create a new audio track.
```python
client.create_audio_track(0)
# → {"track": 0, "count": 4}
```

### `delete_track(index)`
Delete a track by index.
```python
client.delete_track(1)
# → {"deleted": 1}
```

### `duplicate_track(index)`
Duplicate a track.
```python
client.duplicate_track(0)
# → {"duplicated": 0}
```

### `mute_track(index, mute=True)`
Mute or unmute a track.
```python
client.mute_track(0, True)
# → {"track": 0, "mute": true}
```

### `solo_track(index, solo=True)`
Solo or unsolo a track.
```python
client.solo_track(2, True)
# → {"track": 2, "solo": true}
```

### `set_volume(index, volume)`
Set track volume in dB. Range: -60 to +6.
```python
client.set_volume(0, -3.0)
# → {"track": 0, "volume": -3.0}
```

### `set_pan(index, pan)`
Set track pan. Range: -1 (left) to 1 (right).
```python
client.set_pan(0, 0.25)
# → {"track": 0, "pan": 0.25}
```

### `set_send(index, send, value)`
Set a send level. Range: 0-1.
```python
client.set_send(0, 0, 0.5)  # send A to 50%
# → {"track": 0, "send": 0, "value": 0.5}
```

---

## Clips (MIDI)

### `create_midi_clip(track, length_beats=4.0, scene=None)`
Create a new MIDI clip on a track.
```python
client.create_midi_clip(track=0, length_beats=4.0)
# → {"track": 0, "clip": 0, "length_beats": 4.0}
```

### `set_clip_length(track, clip, length_beats)`
Change a clip's length in beats.
```python
client.set_clip_length(track=0, clip=0, length_beats=8.0)
# → {"length_beats": 8.0}
```

### `add_note(track, clip, pitch, start, duration, velocity=100)`
Add a single MIDI note.
- `pitch`: 0-127 (60 = middle C)
- `start`: position in beats (0.0 = start of clip)
- `duration`: length in beats
- `velocity`: 0-127

```python
client.add_note(track=0, clip=0, pitch=60, start=0.0, duration=0.5, velocity=100)
# → {"added": true}
```

### `add_notes(track, clip, notes)`
Add multiple notes at once (more efficient than calling `add_note` repeatedly).
Each note is a tuple: `(pitch, start, duration, velocity)`.
```python
notes = [
    (60, 0.0, 0.5, 100),   # C4 at beat 0
    (64, 0.5, 0.5, 95),    # E4 at beat 0.5
    (67, 1.0, 0.5, 90),    # G4 at beat 1
    (72, 1.5, 1.0, 85),    # C5 at beat 1.5
]
client.add_notes(track=0, clip=0, notes=notes)
# → {"added": 4}
```

### `remove_note(track, clip, pitch, start)`
Remove a note at a specific pitch and start time.
```python
client.remove_note(track=0, clip=0, pitch=60, start=0.0)
# → {"removed": true}
```

### `clear_clip(track, clip)`
Remove all notes from a clip.
```python
client.clear_clip(track=0, clip=0)
# → {"cleared": true}
```

### `quantize_clip(track, clip, grid=4)`
Quantize a clip. Grid is notes-per-beat (4 = 1/16 notes, 8 = 1/32).
```python
client.quantize_clip(track=0, clip=0, grid=4)
# → {"quantized": true}
```

### `toggle_clip_loop(track, clip)`
Toggle the loop state of a clip.
```python
client.toggle_clip_loop(track=0, clip=0)
# → {"loop": false}
```

---

## Browser (loading instruments, effects, samples)

### `load_instrument(track, name)`
Search the Ableton browser for an instrument and load it on a track.
```python
client.load_instrument(track=0, name="Wavetable")
# → {"loaded": "Wavetable", "note": "browser opened — select item to load"}
```

### `load_effect(track, name)`
Load an audio effect on a track.
```python
client.load_effect(track=0, name="Reverb")
# → {"loaded": "Reverb"}
```

### `load_sample(track, name)`
Load a sample on a track.
```python
client.load_sample(track=1, name="kick_808.wav")
# → {"loaded": "kick_808.wav"}
```

### `load_drum_rack(track, name="")`
Load a Drum Rack on a track.
```python
client.load_drum_rack(track=1)
# → {"loaded": "Drum Rack"}
```

---

## Devices

### `set_device_parameter(track, device, param, value)`
Set a parameter on a device. Value range: 0-1.
```python
client.set_device_parameter(track=0, device=0, param=0, value=0.7)
# → {"value": 0.7}
```

### `get_device_parameters(track, device)`
Get all parameters of a device.
```python
client.get_device_parameters(track=0, device=0)
# → {"parameters": [{"name": "Cutoff", "value": 0.7}, ...]}
```

---

## Scenes

### `create_scene(name="")`
Create a new scene.
```python
client.create_scene("Verse 1")
# → {"scene": 1}
```

### `launch_scene(scene)`
Launch a scene by index.
```python
client.launch_scene(0)
# → {"launched": 0}
```

### `reorder_scene(scene, new_index)`
Move a scene to a new position.
```python
client.reorder_scene(2, 0)
# → {"reordered": true}
```

---

## State

### `get_state()`
Returns the last cached state from Ableton (no round-trip — instant).
```python
state = client.get_state()
# → {"connected": true, "state": {"tempo": 120, "playing": false, "tracks": [...], ...}}
```

### `get_full_state()`
Requests a fresh state report from Ableton (one round-trip).
```python
state = client.get_full_state()
# → {"tempo": 120, "playing": false, "tracks": [...], ...}
```

---

## Music Helpers (`hermes/chord_helpers.py`)

### `create_chord_progression(client, track, key, progression_type, bpm)`
Generate and send a chord progression to a track.

Available progressions:
- `I-V-vi-IV` (pop)
- `I-IV-V-I` (classic)
- `ii-V-I` (jazz)
- `I-vi-IV-V`
- `vi-IV-I-V`
- `12-bar-blues`
- `pop`

```python
from hermes.chord_helpers import create_chord_progression

create_chord_progression(client, track=0, key="C", progression_type="I-V-vi-IV", bpm=120)
# → {"key": "C", "progression": "I-V-vi-IV", "bpm": 120, "chords": 4, "clip_length_beats": 16}
```

### `create_drum_pattern(client, track, pattern_name, bpm, bars)`
Generate and send a drum pattern.

Available patterns:
- `four-on-floor` (house, techno)
- `boom-bap` (hip-hop)
- `trap`
- `house`
- `dnb` (drum & bass)
- `breakbeat`

```python
from hermes.chord_helpers import create_drum_pattern

create_drum_pattern(client, track=1, pattern_name="trap", bpm=140, bars=2)
# → {"pattern": "trap", "bpm": 140, "bars": 2, "notes": 28}
```

### `create_scale_melody(client, track, key, scale, num_notes, bpm)`
Generate a melodic sequence from a scale.

Available scales: major, minor, natural_minor, harmonic_minor, dorian, mixolydian, pentatonic_major, pentatonic_minor, blues, chromatic

```python
from hermes.chord_helpers import create_scale_melody

create_scale_melody(client, track=2, key="A", scale="minor", num_notes=8, bpm=120, seed=42)
# → {"key": "A", "scale": "minor", "bpm": 120, "num_notes": 8, "notes": [...]}
```

---

## Complete Example

```python
from hermes.ableton_api import AbletonClient
from hermes.chord_helpers import (
    create_chord_progression, create_drum_pattern, create_scale_melody
)

client = AbletonClient(host="localhost", port=8081, token="your-secret")

# Setup
client.set_tempo(124)
client.create_midi_track(0)  # chords track
client.create_midi_track(1)  # drums track
client.create_midi_track(2)  # melody track

# Load instruments
client.load_instrument(track=0, name="Wavetable")
client.load_drum_rack(track=1)
client.load_instrument(track=2, name="Operator")

# Create content
create_chord_progression(client, track=0, key="C", progression_type="I-V-vi-IV", bpm=124)
create_drum_pattern(client, track=1, pattern_name="four-on-floor", bpm=124, bars=4)
create_scale_melody(client, track=2, key="C", scale="major", num_notes=16, bpm=124, seed=42)

# Tweak parameters
client.set_device_parameter(track=0, device=0, param=0, value=0.8)  # cutoff
client.set_device_parameter(track=1, device=0, param=1, value=0.3)  # reverb

# Mix
client.set_volume(0, -6.0)
client.set_volume(1, -3.0)
client.set_volume(2, -9.0)

# Play
client.launch_scene(0)
client.play()
```
