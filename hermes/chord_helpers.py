"""
Hermes-Ableton Bridge — Music helper functions
==============================================

High-level helpers that generate notes / patterns and send them to Ableton via
an AbletonClient. These are pure-music utilities — no audio synthesis.

Functions
---------
- create_chord_progression(client, track, key, progression_type, bpm)
- create_drum_pattern(client, track, pattern_name)
- create_scale_melody(client, track, key, scale, num_notes)
"""

from __future__ import annotations

import random
from typing import List, Sequence, Tuple

# --------------------------------------------------------------------------- #
#  Music theory constants
# --------------------------------------------------------------------------- #
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Semitone offsets from the root for common scales.
SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "natural_minor": [0, 2, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "pentatonic_major": [0, 2, 4, 7, 9],
    "pentatonic_minor": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
    "chromatic": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
}

# Chord qualities as semitone offsets from a chord root.
CHORD_TYPES = {
    "maj": [0, 4, 7],
    "min": [0, 3, 7],
    "dim": [0, 3, 6],
    "aug": [0, 4, 8],
    "maj7": [0, 4, 7, 11],
    "min7": [0, 3, 7, 10],
    "dom7": [0, 4, 7, 10],
    "sus4": [0, 5, 7],
    "sus2": [0, 2, 7],
    "add9": [0, 4, 7, 14],
}

# Named progressions — Roman-numeral scale degrees (1-indexed) per chord.
PROGRESSIONS = {
    "I-V-vi-IV": [(1, "maj"), (5, "maj"), (6, "min"), (4, "maj")],
    "I-IV-V-I":   [(1, "maj"), (4, "maj"), (5, "maj"), (1, "maj")],
    "ii-V-I":     [(2, "min"), (5, "dom7"), (1, "maj7")],
    "I-vi-IV-V":  [(1, "maj"), (6, "min"), (4, "maj"), (5, "maj")],
    "vi-IV-I-V":  [(6, "min"), (4, "maj"), (1, "maj"), (5, "maj")],
    "12-bar-blues": [(1, "dom7")] * 4 + [(4, "dom7")] * 2 + [(1, "dom7")] * 2 +
                    [(5, "dom7"), (4, "dom7"), (1, "dom7"), (5, "dom7")],
    "pop": [(1, "maj"), (6, "min"), (4, "maj"), (5, "maj")],
}

# General MIDI drum note numbers (standard).
DRUM_KICK = 36
DRUM_SNARE = 38
DRUM_CLOSED_HAT = 42
DRUM_OPEN_HAT = 46
DRUM_CLAP = 39
DRUM_TOM_LOW = 45
DRUM_TOM_MID = 48
DRUM_TOM_HI = 50
DRUM_RIM = 37
DRUM_CYMBAL = 49

# Drum patterns: list of (step, note, velocity). Step is 0-15 (16th notes).
DRUM_PATTERNS = {
    # four-on-floor: kick on every beat, hats on offbeats
    "four-on-floor": [
        (0, DRUM_KICK, 110), (4, DRUM_KICK, 110), (8, DRUM_KICK, 110), (12, DRUM_KICK, 110),
        (2, DRUM_CLOSED_HAT, 80), (6, DRUM_CLOSED_HAT, 80),
        (10, DRUM_CLOSED_HAT, 80), (14, DRUM_CLOSED_HAT, 80),
        (4, DRUM_SNARE, 100), (12, DRUM_SNARE, 100),
    ],
    "boom-bap": [
        (0, DRUM_KICK, 115), (6, DRUM_KICK, 95),
        (8, DRUM_KICK, 110), (11, DRUM_KICK, 90),
        (4, DRUM_SNARE, 120), (12, DRUM_SNARE, 120),
        (2, DRUM_CLOSED_HAT, 70), (6, DRUM_CLOSED_HAT, 70),
        (10, DRUM_CLOSED_HAT, 70), (14, DRUM_CLOSED_HAT, 70),
        (7, DRUM_OPEN_HAT, 60), (15, DRUM_OPEN_HAT, 60),
    ],
    "trap": [
        (0, DRUM_KICK, 120), (3, DRUM_KICK, 100), (7, DRUM_KICK, 105),
        (8, DRUM_KICK, 110), (10, DRUM_KICK, 95), (13, DRUM_KICK, 100),
        (4, DRUM_SNARE, 125), (12, DRUM_SNARE, 125),
        (0, DRUM_CLOSED_HAT, 60), (2, DRUM_CLOSED_HAT, 55), (4, DRUM_CLOSED_HAT, 60),
        (6, DRUM_CLOSED_HAT, 55), (8, DRUM_CLOSED_HAT, 60), (10, DRUM_CLOSED_HAT, 55),
        (12, DRUM_CLOSED_HAT, 60), (14, DRUM_CLOSED_HAT, 55),
        (7, DRUM_OPEN_HAT, 70), (15, DRUM_OPEN_HAT, 70),
    ],
    "house": [
        (0, DRUM_KICK, 115), (4, DRUM_KICK, 115), (8, DRUM_KICK, 115), (12, DRUM_KICK, 115),
        (2, DRUM_OPEN_HAT, 85), (6, DRUM_OPEN_HAT, 85),
        (10, DRUM_OPEN_HAT, 85), (14, DRUM_OPEN_HAT, 85),
        (4, DRUM_CLAP, 105), (12, DRUM_CLAP, 105),
        (2, DRUM_CLOSED_HAT, 60), (6, DRUM_CLOSED_HAT, 60),
        (10, DRUM_CLOSED_HAT, 60), (14, DRUM_CLOSED_HAT, 60),
    ],
    "dnb": [
        (0, DRUM_KICK, 120), (10, DRUM_KICK, 110),
        (4, DRUM_SNARE, 125), (12, DRUM_SNARE, 120),
        (2, DRUM_CLOSED_HAT, 50), (6, DRUM_CLOSED_HAT, 60),
        (10, DRUM_CLOSED_HAT, 55), (14, DRUM_CLOSED_HAT, 65),
    ],
    "breakbeat": [
        (0, DRUM_KICK, 115), (6, DRUM_KICK, 100), (9, DRUM_KICK, 95),
        (4, DRUM_SNARE, 120), (12, DRUM_SNARE, 115),
        (2, DRUM_CLOSED_HAT, 65), (6, DRUM_CLOSED_HAT, 70),
        (10, DRUM_CLOSED_HAT, 65), (14, DRUM_CLOSED_HAT, 70),
        (7, DRUM_OPEN_HAT, 60),
    ],
}


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def note_to_midi(note_name: str, octave: int = 4) -> int:
    """Convert e.g. ('C', 4) -> 60. Octave 4 = middle C (MIDI 60)."""
    name = note_name.strip()
    # handle accidental formats like "C#" or "Db"
    base = name[0].upper()
    rest = name[1:]
    idx = NOTE_NAMES.index(base)
    if rest == "#":
        idx += 1
    elif rest == "b":
        idx -= 1
    elif rest == "":
        pass
    else:
        raise ValueError(f"unknown note name: {note_name}")
    idx %= 12
    return (octave + 1) * 12 + idx


def key_to_root(key: str) -> int:
    """Parse a key like 'C', 'C#', 'C#4' or 'F#3' into a MIDI root note."""
    parts = key.strip().split()
    if len(parts) == 2:
        return note_to_midi(parts[0], int(parts[1]))
    # no octave -> assume octave 3 (low-ish for basslines)
    name = parts[0]
    # strip trailing digits as octave
    oct_idx = 0
    while len(name) > 1 and name[-1].isdigit():
        oct_idx = oct_idx * 10 + int(name[-1])
        name = name[:-1]
    if oct_idx:
        return note_to_midi(name, oct_idx)
    return note_to_midi(name, 3)


def scale_notes(root: int, scale: str, octaves: int = 1) -> List[int]:
    """Return ascending MIDI notes for the given scale/root over N octaves."""
    intervals = SCALES.get(scale.lower())
    if intervals is None:
        raise ValueError(f"unknown scale '{scale}'. Valid: {sorted(SCALES)}")
    notes: List[int] = []
    for o in range(octaves):
        for iv in intervals:
            notes.append(root + o * 12 + iv)
    notes.append(root + octaves * 12)  # top octave root
    return notes


def build_chord(root: int, chord_type: str) -> List[int]:
    intervals = CHORD_TYPES.get(chord_type)
    if intervals is None:
        raise ValueError(f"unknown chord type '{chord_type}'. Valid: {sorted(CHORD_TYPES)}")
    return [root + iv for iv in intervals]


def chord_from_degree(scale_root: int, degree: int, chord_type: str,
                      scale: str = "major") -> List[int]:
    """Build a chord whose root is the Nth degree (1-indexed) of the scale."""
    intervals = SCALES[scale.lower()]
    deg_idx = (degree - 1) % len(intervals)
    chord_root = scale_root + intervals[deg_idx]
    return build_chord(chord_root, chord_type)


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #
def create_chord_progression(client, track: int, key: str,
                             progression_type: str = "I-V-vi-IV",
                             bpm: float = 120.0, scale: str = "major",
                             beats_per_chord: float = 4.0,
                             clip_index: int = 0) -> dict:
    """Create a MIDI clip on `track` containing a chord progression.

    Each chord lasts `beats_per_chord` beats. The clip length is
    beats_per_chord * len(progression).
    """
    root = key_to_root(key)
    prog = PROGRESSIONS.get(progression_type)
    if prog is None:
        raise ValueError(f"unknown progression '{progression_type}'. "
                         f"Valid: {sorted(PROGRESSIONS)}")

    total_beats = beats_per_chord * len(prog)
    client.set_tempo(bpm)
    client.create_midi_clip(track=track, length_beats=total_beats, scene=clip_index)

    notes = []
    t = 0.0
    for degree, ctype in prog:
        chord = chord_from_degree(root, degree, ctype, scale=scale)
        for pitch in chord:
            notes.append((pitch, t, beats_per_chord * 0.95, 95))
        t += beats_per_chord
    client.replace_clip_notes(track=track, clip=clip_index, notes=notes)
    return {"key": key, "progression": progression_type, "bpm": bpm,
            "chords": len(prog), "clip_length_beats": total_beats}


def create_drum_pattern(client, track: int, pattern_name: str = "four-on-floor",
                         bpm: float = 120.0, clip_index: int = 0,
                         bars: int = 1) -> dict:
    """Create a drum clip on `track` with the named pattern, repeated `bars` times.

    Assumes `track` is a MIDI track with a drum rack loaded. Steps are 16th
    notes; one bar = 16 steps = 4 beats.
    """
    steps = DRUM_PATTERNS.get(pattern_name)
    if steps is None:
        raise ValueError(f"unknown drum pattern '{pattern_name}'. "
                         f"Valid: {sorted(DRUM_PATTERNS)}")

    beats_per_bar = 4.0
    total_beats = beats_per_bar * bars
    client.set_tempo(bpm)
    client.create_midi_clip(track=track, length_beats=total_beats, scene=clip_index)
    client.clear_clip(track=track, clip=clip_index)

    notes = []
    for bar in range(bars):
        bar_offset = bar * beats_per_bar
        for step, note, vel in steps:
            start = bar_offset + step * 0.25  # 16th = 0.25 beat
            notes.append((note, start, 0.25, vel))
    client.replace_clip_notes(track=track, clip=clip_index, notes=notes)
    return {"pattern": pattern_name, "bpm": bpm, "bars": bars,
            "notes": len(notes)}


def create_scale_melody(client, track: int, key: str, scale: str = "major",
                         num_notes: int = 8, bpm: float = 120.0,
                         clip_index: int = 0, note_duration: float = 0.5,
                         seed: int = None) -> dict:
    """Generate a melodic sequence from a scale and write it to a clip.

    The melody walks the scale with some random direction changes and occasional
    rests (velocity 0). Deterministic when `seed` is given.
    """
    root = key_to_root(key)
    pool = scale_notes(root, scale, octaves=2)
    if scale.lower() not in SCALES:
        raise ValueError(f"unknown scale '{scale}'. Valid: {sorted(SCALES)}")

    rng = random.Random(seed)
    total_beats = num_notes * note_duration
    client.set_tempo(bpm)
    client.create_midi_clip(track=track, length_beats=total_beats, scene=clip_index)

    idx = len(pool) // 2
    notes: List[Tuple[int, float, float, int]] = []
    for i in range(num_notes):
        # occasionally rest
        if rng.random() < 0.12:
            notes.append((pool[idx], i * note_duration, note_duration, 0))
            continue
        notes.append((pool[idx], i * note_duration, note_duration * 0.9,
                      rng.randint(70, 110)))
        # step up/down the scale, sometimes leap
        step = rng.choice([-2, -1, -1, 1, 1, 2]) if rng.random() < 0.8 else rng.choice([-3, 3])
        idx = max(0, min(len(pool) - 1, idx + step))
    client.replace_clip_notes(track=track, clip=clip_index, notes=notes)
    return {"key": key, "scale": scale, "bpm": bpm, "num_notes": num_notes,
            "notes": notes}
