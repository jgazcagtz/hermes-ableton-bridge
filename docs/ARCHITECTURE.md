# Architecture

## Overview

The Hermes-Ableton Bridge connects a remote AI agent (Hermes, running on a Linux VPS) to Ableton Live (running on a Windows PC) using a WebSocket connection and a Max for Live device.

The key design decision: the WebSocket connection is **outbound from Windows to the VPS**. This means:
- No port forwarding needed on the Windows machine
- No ngrok or NAT traversal required
- Only one port (8080) needs to be open on the VPS
- The connection auto-reconnects if it drops

## Architecture Diagram

```
┌─────────────────────────────────────────────────┐
│  VPS HOSTINGER (Linux)                          │
│                                                 │
│  ┌───────────────┐     HTTP :8081               │
│  │ Hermes Agent   │ ←→  (localhost)              │
│  │ (Python)       │       │                      │
│  └───────────────┘       │                      │
│                          ▼                      │
│                 ┌────────────────┐               │
│                 │ Bridge Server  │               │
│                 │ (ws_server.py) │               │
│                 │                │               │
│                 │ HTTP API:8081  │               │
│                 │ WS Server:8080 │               │
│                 └───────┬────────┘               │
│                         │                        │
└─────────────────────────┼────────────────────────┘
                          │ WebSocket (outbound from Windows)
                          │ ws://VPS_IP:8080
                          │
┌─────────────────────────┼────────────────────────┐
│  WINDOWS (your PC)      │                        │
│                         ▼                        │
│  ┌────────────────────────────────┐              │
│  │ Hermes Bridge.amxd             │              │
│  │ (Max for Live device)          │              │
│  │                                │              │
│  │  ┌──────────┐  ┌────────────┐  │              │
│  │  │ JS engine│←→│ Node for   │  │              │
│  │  │ (LiveAPI)│  │ Max (WS)   │  │              │
│  │  └────┬─────┘  └────────────┘  │              │
│  │       │                         │              │
│  └───────┼─────────────────────────┘              │
│          │ Live Object Model API                 │
│          ▼                                       │
│  ┌────────────────────────────────┐              │
│  │ Ableton Live                   │              │
│  │ (Song, Tracks, Clips, Devices) │              │
│  └────────────────────────────────┘              │
└──────────────────────────────────────────────────┘
```

## Data Flow

### Command Flow (Hermes → Ableton)

1. Hermes agent calls `AbletonClient.play()` in Python
2. `AbletonClient` sends `POST /command {"action": "play"}` to `http://localhost:8081`
3. The bridge server wraps it as a command message with a unique ID
4. The server sends the JSON command over the WebSocket to the M4L device
5. The M4L device's JS engine receives it, calls `LiveAPI("live_set").call("start_play")`
6. The JS engine sends back a response with the same ID
7. The server resolves the pending future and returns the result to Hermes

### State Flow (Ableton → Hermes)

1. The M4L device periodically (every 2 seconds by default) collects full Live state
2. It sends a `state` message over the WebSocket
3. The server caches this state
4. When Hermes calls `client.get_state()`, the server returns the cached state instantly (no round-trip to Ableton)

### Response Routing

Each command gets a UUID. The server creates an `asyncio.Future` keyed by this UUID. When the response arrives from Ableton, the server resolves the corresponding future. This allows multiple concurrent commands and proper timeout handling.

## Message Protocol

### Command Message (VPS → Ableton)

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "command",
  "action": "play",
  "params": {},
  "timestamp": 1697200000
}
```

### Response Message (Ableton → VPS)

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "response",
  "status": "ok",
  "data": { "playing": true },
  "error": null,
  "timestamp": 1697200000
}
```

Error response:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "response",
  "status": "error",
  "data": {},
  "error": "track index out of range",
  "timestamp": 1697200000
}
```

### State Report (Ableton → VPS, periodic)

```json
{
  "type": "state",
  "data": {
    "tempo": 120.0,
    "playing": false,
    "loop": false,
    "metronome": false,
    "overdub": false,
    "time_signature": [4, 4],
    "tracks": [
      {
        "index": 0,
        "name": "1 MIDI",
        "type": "midi",
        "volume": 0.0,
        "pan": 0.0,
        "mute": false,
        "solo": false,
        "clips": [
          {
            "index": 0,
            "length_beats": 4.0,
            "loop": true,
            "notes": []
          }
        ],
        "devices": [
          {
            "index": 0,
            "name": "Wavetable",
            "type": "instrument",
            "parameters": []
          }
        ]
      }
    ],
    "scenes": [
      { "index": 0, "name": "Scene 1" }
    ]
  },
  "timestamp": 1697200000
}
```

### Auth Message (Ableton → VPS, first message)

```json
{ "auth": "your-secret-token" }
```

Auth response (VPS → Ableton):
```json
{ "type": "auth", "status": "ok" }
```

## Components

### Bridge Server (`server/ws_server.py`)

Two servers in one process:
- **WebSocket server** (port 8080): accepts connections from the M4L device. Only one Ableton connection at a time (new connections replace old ones).
- **HTTP API** (port 8081): accepts commands from the Hermes agent. Multiple concurrent HTTP clients supported.

The server uses `asyncio` for concurrency. Commands are routed via UUID futures. State is cached and updated on every state report from Ableton.

### Max for Live Device

Three components inside the `.amxd`:
- **`hermes_bridge.js`**: JavaScript engine that runs inside Max's `js` object. Handles command dispatch via the LiveAPI object, state reporting, and connection lifecycle.
- **`hermes_bridge_node.js`**: Node for Max script that owns the WebSocket connection (Max's `js` object can't do networking). Exchanges JSON via shared dicts (`ws_in`, `ws_out`) to avoid Max's symbol/space issues with raw JSON strings.
- **`hermes_bridge.amxd`**: The device wrapper (Max patcher JSON) that wires the JS and Node objects together with config dicts and UI buttons.

### Hermes API (`hermes/ableton_api.py`)

Synchronous Python client that sends HTTP requests to the bridge server. No async needed — just call methods and get results. Uses `urllib` (no external deps beyond stdlib).

## Performance Notes

- Command round-trip latency: ~50-200ms depending on network (VPS location vs your PC)
- State reports: every 2 seconds (configurable)
- State queries (`get_state`): instant (cached, no round-trip)
- Full state queries (`get_full_state`): one round-trip to Ableton
- The WebSocket is a single connection — commands are serialized. Don't send rapid-fire real-time MIDI notes; use `add_notes` (batch) instead.
