# Hermes-Ableton Bridge

Remote-control Ableton Live from a Hermes AI agent running on a Linux VPS, via WebSocket + OSC.

## What it does

Hermes (on your VPS) sends commands like `play`, `set_tempo(120)`, `create_midi_clip`, `add_note`, `load_instrument("Serum")` — and Ableton Live (on your Windows PC) executes them in real time through a Python bridge + AbletonOSC.

Full control: transport, tracks, MIDI clips, notes, instruments, effects, device parameters, scenes, and live state reporting.

## Architecture

```
┌─────────────────────────────────────────┐
│  VPS HOSTINGER (Linux)                  │
│                                         │
│  Hermes Agent                           │
│    ↕ (localhost HTTP :8081)             │
│  WebSocket Server (:8080)               │
│    ↕ (WAN — one outbound WS connection) │
└─────────────────────────────────────────┘
                  ↕
┌─────────────────────────────────────────┐
│  WINDOWS (your PC)                      │
│                                         │
│  Python Bridge (ableton_osc_bridge.py)  │
│    - WebSocket client → VPS:8080        │
│    - JSON commands → OSC                │
│    - State reports → VPS               │
│    ↕ (OSC localhost)                    │
│  AbletonOSC (M4L device in Ableton)     │
│    - Listens on port 11000              │
│    - Responds on port 11001            │
│  Ableton Live                           │
└─────────────────────────────────────────┘
```

Key design: the WebSocket connection is **outbound from Windows** to the VPS. No ngrok, no port forwarding on your Windows machine. Just open port 8080 on the VPS firewall.

The Windows side uses two components:
1. **AbletonOSC** — a real, downloadable Max for Live device (<https://github.com/ideoforms/AbletonOSC>) that exposes the Live Object Model via OSC
2. **Python bridge script** (`bridge/ableton_osc_bridge.py`) — translates between the VPS WebSocket protocol and AbletonOSC's OSC protocol

## Quick Start

### VPS side (Linux)

```bash
cd /opt/data/workspace/hermes-ableton-bridge
uv pip install -r server/requirements.txt
cp server/config.yaml server/config.local.yaml
# Edit config.local.yaml — set auth_token to something secret
python server/ws_server.py --config server/config.local.yaml
```

Open the firewall:
```bash
# UFW example
sudo ufw allow 8080/tcp
```

Find your VPS public IP:
```bash
curl ifconfig.me
```

### Windows side (Ableton + Python bridge)

**Step 1 — Install AbletonOSC** (Max for Live device):
1. Download from <https://github.com/ideoforms/AbletonOSC> (Releases page → `.amxd` file)
2. Open Ableton Live, drag the `.amxd` onto a track
3. Verify it listens on port 11000 and responds on 11001

**Step 2 — Install Python + deps**:
```powershell
cd C:\path\to\hermes-ableton-bridge\bridge
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**Step 3 — Configure** (`bridge/config.yaml`):
```yaml
vps_host: "YOUR_VPS_PUBLIC_IP"
auth_token: "THE_SAME_TOKEN_AS_SERVER"
```

**Step 4 — Run the bridge**:
```powershell
python ableton_osc_bridge.py
```

The bridge connects to the VPS automatically. You should see "Authenticated with VPS".

> **Test without Ableton:** `python ableton_osc_bridge.py --dry-run` uses an in-memory mock Live set.

See [`bridge/README.md`](bridge/README.md) for full bridge documentation.

### Test without Ableton (mock client)

```bash
# Terminal 1 — start the server
cd /opt/data/workspace/hermes-ableton-bridge
python server/ws_server.py --config server/config.yaml

# Terminal 2 — run the mock client (simulates Ableton)
python tests/mock_client.py

# Terminal 3 — test via the API
python -m hermes.ableton_api status
python -m hermes.ableton_api play
python -m hermes.ableton_api set_tempo
```

Or run the full test suite:
```bash
cd /opt/data/workspace/hermes-ableton-bridge
uv pip install -r requirements.txt
uv run pytest tests/ -v
```

## Repository structure

```
hermes-ableton-bridge/
├── bridge/                # Python OSC bridge (runs on Windows alongside Ableton)
│   ├── ableton_osc_bridge.py      # Main bridge script: WebSocket ↔ OSC
│   ├── config.yaml                # Bridge configuration
│   ├── requirements.txt           # python-osc, websockets, pyyaml
│   └── README.md                  # Bridge setup guide
├── server/                # WebSocket server (runs on VPS)
│   ├── ws_server.py                # WS :8080 + HTTP API :8081
│   ├── config.yaml                 # Server configuration
│   └── requirements.txt
├── hermes/                # Python API (Hermes agent calls this)
│   ├── ableton_api.py              # AbletonClient — full command API
│   ├── chord_helpers.py            # Chord progressions, drum patterns, melodies
│   └── requirements.txt
├── max-for-live/          # Legacy M4L device (deprecated — use bridge/ + AbletonOSC instead)
│   ├── hermes_bridge.amxd          # Custom .amxd (Ableton refused to load hand-crafted files)
│   ├── hermes_bridge.js            # Legacy JS engine
│   └── hermes_bridge_node.js       # Legacy Node for Max WebSocket client
├── tests/                 # Testing tools
│   ├── mock_client.py             # Simulates Ableton (no Ableton needed)
│   ├── test_api.py                # Integration tests (20 tests)
│   └── test_protocol.py           # Protocol tests
├── docs/                  # Documentation
│   ├── INSTALLATION.md
│   ├── ARCHITECTURE.md
│   ├── COMMANDS.md
│   ├── MAX-FOR-LIVE.md
│   └── TROUBLESHOOTING.md
├── requirements.txt       # All Python deps
├── config.example.yaml    # Example config
└── .gitignore
```

> **Note:** The `max-for-live/` folder contains the deprecated custom `.amxd` approach that didn't work (Ableton doesn't accept hand-crafted JSON as a valid device file). Use `bridge/` + AbletonOSC instead.

## Quick API example

```python
from hermes.ableton_api import AbletonClient
from hermes.chord_helpers import create_chord_progression, create_drum_pattern

client = AbletonClient(host="localhost", port=8081, token="your-secret-token")

# Transport
client.play()
client.set_tempo(124)

# Create a MIDI track and add a chord progression
client.create_midi_track(0)
create_chord_progression(client, track=0, key="C", progression_type="I-V-vi-IV", bpm=124)

# Create a drum track
client.create_midi_track(1)
client.load_drum_rack(track=1)
create_drum_pattern(client, track=1, pattern_name="four-on-floor", bpm=124)

# Load an instrument and tweak it
client.load_instrument(track=0, name="Wavetable")
client.set_device_parameter(track=0, device=0, param=0, value=0.7)

# Launch
client.launch_scene(0)
```

## Security

- All WebSocket connections require a shared secret token (first message auth)
- The HTTP API listens on localhost only (127.0.0.1) by default
- For WAN SSL, enable SSL in the server config and set `use_ssl: true` in the bridge `config.yaml`
- Never commit your real auth token to git

## Requirements

- **VPS**: Python 3.11+, websockets, aiohttp, pyyaml
- **Windows**: Ableton Live 11+ with Max for Live, Python 3.8+, python-osc, websockets, pyyaml, and the AbletonOSC device installed
- **Network**: VPS port 8080 open (TCP inbound)

## License

MIT — built for Jorge Gasca's Hermes Agent setup.
