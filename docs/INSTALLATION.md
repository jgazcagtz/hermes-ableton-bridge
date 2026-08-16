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

## Part 2: Windows Side (Ableton Live)

### 2.1 Prerequisites

- Ableton Live 11 or 12 (Standard, Suite, or Intro with Max for Live)
- Max for Live installed (comes with Suite; separate download for Standard)
- Node.js installed on Windows (for Node for Max)
  - Download from https://nodejs.org (LTS version)
  - Verify: open Command Prompt, run `node --version`

### 2.2 Copy the Max for Live files

Copy the entire `max-for-live/` folder from the repo to your Windows machine, e.g.:
```
C:\Users\Jorge\Documents\Ableton\User Library\Presets\Max for Live\Hermes Bridge\
```

The folder should contain:
- `hermes_bridge.amxd`
- `hermes_bridge_inner.maxpat`
- `hermes_bridge.maxpat`
- `hermes_bridge.js`
- `hermes_bridge_node.js`
- `package.json`

### 2.3 Install the `ws` npm package for Node for Max

1. Open Ableton Live
2. Go to: Options → Preferences → Max for Live (or look in the Max editor)
3. Find "Node for Max" settings
4. Open the Node for Max package folder
5. Open Command Prompt in that folder and run:
   ```
   npm install ws
   ```

Alternatively, if you copied the `max-for-live/` folder somewhere, open Command Prompt there and run:
```
npm install
```

### 2.4 Load the device in Ableton

1. Open Ableton Live
2. Create a new project or open an existing one
3. In the browser (left panel), navigate to:
   - Max for Live → Max for Live (or your User Library if you copied there)
   - Or simply drag `hermes_bridge.amxd` from File Explorer into a MIDI track
4. The device should appear on the track

### 2.5 Configure the device

1. In the device, find the `config` dict object
2. Double-click it to open the dict editor
3. Set the values:
   ```json
   {
     "vps_host": "YOUR_VPS_PUBLIC_IP",
     "vps_port": 8080,
     "auth_token": "THE_SAME_TOKEN_AS_SERVER",
     "use_ssl": 0,
     "state_interval": 2.0
   }
   ```
4. Close the dict editor

### 2.6 Connect

1. Click the **Connect** button in the device
2. Open the Max console (View → Max Console, or click the console icon)
3. You should see:
   ```
   [hermes] config loaded: YOUR_VPS_IP:8080
   [hermes] connecting to ws://YOUR_VPS_IP:8080 ...
   [hermes] socket open — authenticating
   [hermes] authenticated
   ```

If you see "authenticated" — you're connected!

On the VPS server side, you should see:
```
Ableton client connected: (<your-home-ip>, <port>)
```

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

- The `auth_token` must match between the server config and the M4L device
- The HTTP API (port 8081) listens on localhost only by default — only the Hermes agent on the VPS can call it
- The WebSocket server (port 8080) is open to the internet — the token is your only protection
- For production: enable SSL in the server config and use `wss://` in the M4L device
- Consider restricting port 8080 to your home IP in the firewall for extra security
- Never commit `config.local.yaml` with real tokens to git

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common issues and fixes.
