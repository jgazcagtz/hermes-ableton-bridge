# Full Installation Guide

## Part 1: VPS Side (Linux / Hostinger)

### 1.1 Install dependencies

```bash
cd /opt/data/workspace/hermes-ableton-bridge
uv pip install -r server/requirements.txt
uv pip install -r hermes/requirements.txt
```

Or with pip in a venv:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
pip install -r hermes/requirements.txt
```

### 1.2 Configure the server

```bash
cp server/config.yaml server/config.local.yaml
```

Edit `server/config.local.yaml`:
```yaml
ws_host: "0.0.0.0"
ws_port: 8080
http_host: "127.0.0.1"    # localhost only — Hermes calls this
http_port: 8081
auth_token: "GENERATE_A_SECRET_HERE"   # use a long random string
command_timeout: 10.0
state_interval: 2.0
log_level: "INFO"
ssl:
  enabled: false
```

Generate a secure token:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 1.3 Open the firewall

The WebSocket server listens on port 8080. Your Windows Ableton connects to this port.

**UFW (Ubuntu/Debian):**
```bash
sudo ufw allow 8080/tcp
sudo ufw status
```

**Hostinger VPS (if using Hostinger's firewall panel):**
- Go to hPanel → VPS → Firewall
- Add a rule: TCP port 8080, allow from any IP (or restrict to your home IP for extra security)

**iptables (manual):**
```bash
sudo iptables -A INPUT -p tcp --dport 8080 -j ACCEPT
sudo iptables-save
```

### 1.4 Find your VPS public IP

```bash
curl ifconfig.me
```
Note this IP — you'll enter it in the Max for Live device.

### 1.5 Start the server

```bash
python server/ws_server.py --config server/config.local.yaml
```

You should see:
```
WebSocket server listening on 0.0.0.0:8080 (ssl=False)
HTTP API listening on 127.0.0.1:8081
```

For production, run with a process manager:
```bash
# Using nohup (simple)
nohup python server/ws_server.py --config server/config.local.yaml &

# Or create a systemd service (recommended)
```

### 1.6 Verify the server is reachable

From your Windows machine, open a browser and go to:
```
http://YOUR_VPS_IP:8080
```
You should get a connection (even if it's a 426 Upgrade Required — that means the port is open).

---

## Part 2: Windows Side (Ableton Live + Python Bridge)

The Windows side uses two components:

1. **AbletonOSC** — a real, downloadable Max for Live device that exposes the
   Live Object Model over OSC (replaces the old custom `.amxd` device)
2. **Python bridge script** — translates between the VPS WebSocket protocol and
   AbletonOSC's OSC protocol (replaces the old Node.js engine inside Max)

### 2.1 Prerequisites

- Ableton Live 11 or 12 (Standard, Suite, or Intro with Max for Live)
- Max for Live installed (comes with Suite; separate download for Standard)
- **Python 3.8+** installed on Windows
  - Download from <https://python.org> (check "Add Python to PATH" during install)
  - Verify: open Command Prompt, run `python --version`

### Step 1: Install AbletonOSC (Max for Live device)

AbletonOSC is a free, open-source Max for Live device by ideoforms that exposes
the Live Object Model via OSC. This replaces the custom `.amxd` that didn't work.

1. Download it from **<https://github.com/ideoforms/AbletonOSC>**
   - Go to the Releases page and download the latest `.amxd` file
2. Open Ableton Live
3. Drag the AbletonOSC `.amxd` file onto any track
   (or use Add Device → Max for Live → AbletonOSC)
4. AbletonOSC **listens on port 11000** by default and **sends responses to
   port 11001**. Verify these in the device UI.

> AbletonOSC is a real, working device maintained by the community — unlike the
> custom hand-crafted `.amxd` file which Ableton refused to load.

### Step 2: Install Python + dependencies on Windows

1. Copy the `bridge/` folder from the repo to your Windows machine, e.g.:
   ```
   C:\Users\Jorge\hermes-ableton-bridge\bridge\
   ```

2. Open Command Prompt or PowerShell:
   ```powershell
   cd C:\Users\Jorge\hermes-ableton-bridge\bridge

   # Create a virtual environment (recommended)
   python -m venv .venv
   .venv\Scripts\activate

   # Install dependencies
   pip install -r requirements.txt
   ```

   This installs `python-osc`, `websockets`, and `pyyaml`.

### Step 3: Configure the bridge

Edit `bridge/config.yaml`:

```yaml
vps_host: "YOUR_VPS_PUBLIC_IP"     # e.g. 177.7.34.85
vps_port: 8080
auth_token: "THE_SAME_TOKEN_AS_SERVER"   # must match server config
osc_host: "127.0.0.1"
osc_send_port: 11000               # AbletonOSC listens here
osc_listen_port: 11001             # AbletonOSC sends responses here
state_interval: 2.0
```

The `auth_token` **must match** the server's `auth_token` in
`server/config.local.yaml`.

### Step 4: Run the bridge script

```powershell
cd C:\Users\Jorge\hermes-ableton-bridge\bridge
.venv\Scripts\activate
python ableton_osc_bridge.py
```

You should see:
```
[INFO] Connecting to ws://YOUR_VPS_IP:8080 ...
[INFO] Authenticated with VPS at ws://YOUR_VPS_IP:8080
[INFO] State polling started (every 2.0s)
```

If you see "Authenticated" — you're connected! The bridge automatically:
- Translates VPS commands into OSC for AbletonOSC
- Sends responses back to the VPS
- Reports Ableton state every 2 seconds

The bridge connects to the VPS automatically — no manual connection needed.

> **Tip:** Test without Ableton using `python ableton_osc_bridge.py --dry-run`.
> This uses an in-memory mock Live set so you can verify the VPS connection
> without Ableton running.

### 2.2 Verify the connection

On the VPS server side, you should see:
```
Ableton client connected: (<your-home-ip>, <port>)
```

Then test commands from the Hermes agent:
```bash
python -m hermes.ableton_api status
python -m hermes.ableton_api play
python -m hermes.ableton_api set_tempo
```

See [`bridge/README.md`](../bridge/README.md) for full bridge documentation,
including OSC path reference and troubleshooting.

---

## Part 3: Testing Without Ableton

You can test the full system without Ableton using the mock client.

### 3.1 Start the server (VPS)

```bash
cd /opt/data/workspace/hermes-ableton-bridge
python server/ws_server.py --config server/config.yaml
```

### 3.2 Start the mock client (VPS, second terminal)

```bash
cd /opt/data/workspace/hermes-ableton-bridge
python tests/mock_client.py
```

This simulates a full Ableton Live set in memory. It:
- Connects to the WebSocket server
- Authenticates
- Responds to all commands (play, stop, create clips, add notes, etc.)
- Sends periodic state reports

### 3.3 Test commands (VPS, third terminal)

```bash
cd /opt/data/workspace/hermes-ableton-bridge

# Check status
python -m hermes.ableton_api status

# Play
python -m hermes.ableton_api play

# Set tempo
python -m hermes.ableton_api set_tempo

# Get full state
python -m hermes.ableton_api get_state
```

### 3.4 Run the test suite

```bash
cd /opt/data/workspace/hermes-ableton-bridge
uv pip install -r requirements.txt
uv run pytest tests/ -v
```

All 20 tests should pass.

---

## Part 4: Security Notes

- The `auth_token` must match between the server config and the bridge `config.yaml`
- The HTTP API (port 8081) listens on localhost only by default — only the Hermes agent on the VPS can call it
- The WebSocket server (port 8080) is open to the internet — the token is your only protection
- For production: enable SSL in the server config and set `use_ssl: true` in the bridge `config.yaml`
- Consider restricting port 8080 to your home IP in the firewall for extra security
- Never commit `config.local.yaml` or `bridge/config.yaml` with real tokens to git

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common issues and fixes.
