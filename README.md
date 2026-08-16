# Hermes-Ableton Bridge

Remote-control Ableton Live from a Hermes AI agent running on a Linux VPS, via WebSocket + Max for Live.

## What it does

Hermes (on your VPS) sends commands like `play`, `set_tempo(120)`, `create_midi_clip`, `add_note`, `load_instrument("Serum")` — and Ableton Live (on your Windows PC) executes them in real time through a Max for Live device.

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
│  Ableton Live                           │
│    ↕ (Max for Live API)                 │
│  Hermes Bridge.amxd (M4L device)        │
│    - WebSocket client → VPS:8080        │
│    - JSON commands → Live API           │
│    - State reports → VPS               │
└─────────────────────────────────────────┘
```

Key design: the WebSocket connection is **outbound from Windows** to the VPS. No ngrok, no port forwarding on your Windows machine. Just open port 8080 on the VPS firewall.

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

### Windows side (Ableton)

1. Copy the `max-for-live/` folder to your Windows machine
2. Open Ableton Live
3. Drag `hermes_bridge.amxd` into a MIDI track (or use Add Device → Max for Live)
4. In the device, edit the `config` dict:
   - `vps_host`: your VPS public IP
   - `auth_token`: the same token you set in the server config
   - `vps_port`: 8080
5. Install the `ws` npm package for Node for Max:
   - In Max: menu → Node for Max → Open Package Folder
   - Run: `npm install ws`
6. Click the **Connect** button in the device
7. Check the Max console — you should see `[hermes] authenticated`

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
├── max-for-live/          # Max for Live device (runs in Ableton on Windows)
│   ├── hermes_bridge.amxd          # M4L device file (drag into Ableton)
│   ├── hermes_bridge_inner.maxpat  # Inner patch (referenced by .amxd)
│   ├── hermes_bridge.maxpat        # Standalone Max patch (for debugging)
│   ├── hermes_bridge.js            # JS engine: Live API command dispatch
│   ├── hermes_bridge_node.js       # Node for Max: WebSocket client
│   └── package.json                # npm dep for ws package
├── server/                # WebSocket server (runs on VPS)
│   ├── ws_server.py                # WS :8080 + HTTP API :8081
│   ├── config.yaml                 # Server configuration
│   └── requirements.txt
├── hermes/                # Python API (Hermes agent calls this)
│   ├── ableton_api.py              # AbletonClient — full command API
│   ├── chord_helpers.py            # Chord progressions, drum patterns, melodies
│   └── requirements.txt
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
- For WAN SSL, enable SSL in the server config and use `wss://` in the M4L device
- Never commit your real auth token to git

## Requirements

- **VPS**: Python 3.11+, websockets, aiohttp, pyyaml
- **Windows**: Ableton Live 11+ with Max for Live, Node.js (for Node for Max)
- **Network**: VPS port 8080 open (TCP inbound)

## License

MIT — built for Jorge Gasca's Hermes Agent setup.
