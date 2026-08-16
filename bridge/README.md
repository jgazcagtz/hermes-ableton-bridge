# Hermes-Ableton OSC Bridge (Python)

Python bridge that connects the Hermes VPS WebSocket server to **AbletonOSC** —
a real Max for Live device that exposes the Live Object Model over OSC.

```
VPS (WebSocket :8080)  ←─WebSocket─→  Python Bridge  ←─OSC─→  AbletonOSC (Ableton)
                        (outbound)                   (localhost)
```

The bridge runs on your **Windows PC** alongside Ableton. It replaces the old
custom `.amxd` + Node.js approach with a single Python script.

---

## Prerequisites

- **Ableton Live 11/12** with Max for Live (Suite, or Standard + M4L license)
- **Python 3.8+** on your Windows PC ([python.org](https://python.org))
- The **VPS server** already running (`server/ws_server.py`) with port 8080 open

---

## Step 1: Install AbletonOSC

AbletonOSC is a free, open-source Max for Live device by ideoforms.

1. Download it from **<https://github.com/ideoforms/AbletonOSC>**
   - Go to the Releases page and download the latest `.amxd` file
   - Or clone the repo and follow their build instructions
2. Open Ableton Live
3. Drag the AbletonOSC `.amxd` file onto any track (or use
   Add Device → Max for Live → AbletonOSC)
4. By default, AbletonOSC **listens on port 11000** and **sends responses to
   port 11001**. Verify these in the device UI (you can change them if needed,
   but then also update `config.yaml`).

> **Important:** AbletonOSC must be loaded on a track and its server must be
> running (there's usually a "Start" or it auto-starts). Check the AbletonOSC
> documentation for details.

---

## Step 2: Install Python + dependencies

Open a terminal (Command Prompt or PowerShell) on Windows:

```powershell
cd C:\path\to\hermes-ableton-bridge\bridge

# Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

This installs:
- `python-osc` — sends/receives OSC messages
- `websockets` — WebSocket client to connect to the VPS
- `pyyaml` — reads `config.yaml`

---

## Step 3: Configure

Edit `config.yaml` in the `bridge/` folder:

```yaml
vps_host: "YOUR_VPS_PUBLIC_IP"     # e.g. 177.7.34.85
vps_port: 8080
auth_token: "THE_SAME_TOKEN_AS_SERVER"
osc_host: "127.0.0.1"
osc_send_port: 11000
osc_listen_port: 11001
state_interval: 2.0
```

The `auth_token` **must match** the server's `auth_token` in
`server/config.yaml` (or `config.local.yaml`).

You can also override any setting via command-line args or environment
variables (see below).

---

## Step 4: Run the bridge

```powershell
# From the bridge/ directory
python ableton_osc_bridge.py
```

You should see:
```
[INFO] ableton-bridge: ============================================================
[INFO] ableton-bridge: Hermes-Ableton OSC Bridge
[INFO] ableton-bridge:   VPS:        ws://177.7.34.85:8080
[INFO] ableton-bridge:   OSC send:    127.0.0.1:11000
[INFO] ableton-bridge:   OSC listen:  127.0.0.1:11001
[INFO] ableton-bridge:   State poll:  every 2.0s
[INFO] ableton-bridge:   Dry-run:     False
[INFO] ableton-bridge: ============================================================
[INFO] ableton-bridge: OSC sender → 127.0.0.1:11000
[INFO] ableton-bridge: OSC listener ← 127.0.0.1:11001
[INFO] ableton-bridge: OSC server listening on 127.0.0.1:11001
[INFO] ableton-bridge: Connecting to ws://177.7.34.85:8080 ...
[INFO] ableton-bridge: Authenticated with VPS at ws://177.7.34.85:8080
[INFO] ableton-bridge: State polling started (every 2.0s)
```

If you see "Authenticated with VPS" — you're connected!

---

## Dry-run mode (no Ableton needed)

Test the bridge without Ableton running. It uses an in-memory mock Live set:

```powershell
python ableton_osc_bridge.py --dry-run
```

In dry-run mode:
- No OSC is sent (no AbletonOSC needed)
- An in-memory mock responds to all commands
- It still connects to the VPS WebSocket server and processes real commands
- State reports come from the mock Live set

This is useful for verifying the VPS connection and command flow.

---

## Command-line arguments

```
python ableton_osc_bridge.py [OPTIONS]

  --config PATH              Path to config.yaml (default: ./config.yaml)
  --vps-host HOST            VPS WebSocket host
  --vps-port PORT            VPS WebSocket port
  --token TOKEN              Auth token
  --osc-host HOST            AbletonOSC host (default: 127.0.0.1)
  --osc-port PORT            AbletonOSC send port (default: 11000)
  --osc-listen-port PORT     OSC response listen port (default: 11001)
  --state-interval SECS      State poll interval (default: 2.0)
  --dry-run                  No real OSC — mock mode
```

All arguments override `config.yaml` values.

---

## How it connects to the VPS

1. The bridge opens an **outbound WebSocket** connection to
   `ws://VPS_HOST:8080`
2. It sends `{"auth": "TOKEN"}` as the first message
3. The VPS server responds `{"type": "auth", "status": "ok"}`
4. The bridge then listens for JSON command messages
5. Each command is translated to OSC, sent to AbletonOSC, and the response is
   sent back to the VPS as JSON
6. Every `state_interval` seconds, the bridge queries Ableton state and sends
   a state report to the VPS

The connection is **outbound from Windows** — no port forwarding or ngrok needed
on the Windows side. Only the VPS needs port 8080 open.

If the connection drops, the bridge auto-reconnects with exponential backoff
(1s → 2s → 4s → ... → max 30s).

---

## Supported commands

All the same commands as the original bridge:

| Category  | Commands |
|-----------|----------|
| Transport | `play`, `stop`, `set_tempo`, `set_time_signature`, `toggle_loop`, `toggle_metronome`, `overdub` |
| Tracks    | `create_midi_track`, `create_audio_track`, `delete_track`, `duplicate_track`, `mute_track`, `solo_track`, `set_volume`, `set_pan`, `set_send` |
| Clips     | `create_midi_clip`, `set_clip_length`, `add_note`, `add_notes`, `remove_note`, `clear_clip`, `quantize_clip`, `toggle_clip_loop` |
| Browser   | `load_instrument`, `load_effect`, `load_sample`, `load_drum_rack` |
| Devices   | `set_device_parameter`, `get_device_parameters` |
| Scenes    | `create_scene`, `launch_scene`, `reorder_scene` |
| State     | `get_full_state` |

---

## OSC path reference

The bridge sends OSC to AbletonOSC following the Live Object Model convention.
If your AbletonOSC version uses different paths, you can adjust them in the
bridge script (the `_cmd_*` methods). Common paths:

| Action | OSC path | Args |
|--------|----------|------|
| Play | `/live_set/start_play` | — |
| Stop | `/live_set/stop_play` | — |
| Set tempo | `/live_set/tempo` | `[bpm]` |
| Set time sig | `/live_set/signature_numerator` + `/live_set/signature_denominator` | `[int]` |
| Create MIDI track | `/live_set/create_midi_track` | `[index]` |
| Set volume | `/live_set/tracks/{i}/volume` | `[float]` |
| Set pan | `/live_set/tracks/{i}/pan` | `[float]` |
| Create clip | `/live_set/tracks/{i}/clip_slots/{s}/create_clip` | `[length]` |
| Add note | `/live_set/tracks/{i}/clip_slots/{s}/clip/add_notes` | `[pitch, start, dur, vel, mute]` |
| Launch scene | `/live_set/scenes/{s}/launch` | — |

> **Note:** AbletonOSC's exact OSC paths may vary between versions. The bridge
> includes fallback patterns for common alternatives. If a command doesn't work,
> check the AbletonOSC console/log to see what paths it expects, and adjust the
> `_cmd_*` methods in `ableton_osc_bridge.py`.

---

## Troubleshooting

**"Authentication failed"** — The `auth_token` in `config.yaml` doesn't match
the server's token. Double-check both.

**"OSC response timeout"** — AbletonOSC isn't running or isn't reachable on
`127.0.0.1:11000`. Make sure the AbletonOSC device is loaded and its server is
started in Ableton.

**"Connection refused" (WebSocket)** — The VPS server isn't running or port 8080
isn't open. Check the firewall.

**Commands don't affect Ableton** — Try `--dry-run` first to verify the VPS
connection works. Then check AbletonOSC's logs to see if it's receiving OSC
messages. You may need to adjust OSC paths for your AbletonOSC version.

**Port 11001 already in use** — Something else is listening on 11001. Change
`osc_listen_port` in config.yaml AND configure AbletonOSC to send responses to
the new port.
