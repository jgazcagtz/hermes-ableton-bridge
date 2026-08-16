# Troubleshooting

## Connection Issues

### "Connection refused" / can't connect from Ableton

**Symptom:** The Max console shows `connecting to ws://VPS_IP:8080 ...` but never shows `socket open`.

**Causes & fixes:**

1. **VPS firewall not open**
   ```bash
   # Check if port 8080 is open on the VPS
   sudo ufw status
   # If not open:
   sudo ufw allow 8080/tcp
   ```

2. **Hostinger VPS firewall panel**
   - Go to hPanel → VPS → Firewall
   - Make sure TCP 8080 is allowed

3. **Server not running**
   ```bash
   # Check if ws_server.py is running
   ps aux | grep ws_server
   # If not, start it:
   python server/ws_server.py --config server/config.local.yaml
   ```

4. **Wrong IP in the config dict**
   - In the Max for Live device, check `vps_host` is your VPS public IP
   - Find it: `curl ifconfig.me` on the VPS
   - Don't use `localhost` or `127.0.0.1` — that refers to your Windows machine

5. **Port mismatch**
   - Make sure `vps_port` in the M4L device matches `ws_port` in `server/config.yaml`

### "Auth failed" / authentication error

**Symptom:** Max console shows `auth FAILED: bad token`.

**Fix:** The `auth_token` in the M4L device's config dict must exactly match the `auth_token` in `server/config.yaml`. Copy-paste to avoid typos.

### Connection drops / reconnects frequently

**Symptom:** The device connects, then disconnects, then reconnects in a loop.

**Causes & fixes:**

1. **Unstable network** — The WebSocket has built-in reconnect with backoff. If your internet is flaky, it will keep trying. Check your connection.

2. **VPS resource limits** — Make sure the VPS has enough memory/CPU. Check with `htop` or `free -m`.

3. **Ping timeout** — The server sends WebSocket pings every 20 seconds. If your network blocks pings, increase the timeout in `ws_server.py`:
   ```python
   ws_srv = await websockets.serve(..., ping_interval=60, ping_timeout=60)
   ```

---

## Max for Live Issues

### "Module not found: ws" / Node for Max error

**Symptom:** Max console shows an error about the `ws` module not being found.

**Fix:** Install the `ws` npm package:
1. Find the Node for Max package directory (Options → Preferences → Max for Live)
2. Open Command Prompt there
3. Run: `npm install ws`

Or install it in the device folder:
1. Navigate to the folder where you copied the `max-for-live/` files
2. Open Command Prompt there
3. Run: `npm install`

### Device doesn't load / appears blank

**Symptom:** You drag the .amxd into Ableton but it shows nothing or an error.

**Fix:**
1. Make sure ALL these files are in the SAME folder:
   - `hermes_bridge.amxd`
   - `hermes_bridge_inner.maxpat`
   - `hermes_bridge.js`
   - `hermes_bridge_node.js`
2. Check that you have a valid Max for Live license (comes with Ableton Suite)
3. Try opening the `.maxpat` file directly in Max (outside Ableton) to debug

### "LiveAPI: not found" / Live API errors

**Symptom:** Commands fail with errors like "path not found" or "property not found".

**Causes & fixes:**
- You're trying to access a track/clip index that doesn't exist. Check with `get_state()` first.
- Some Live API properties differ between Ableton versions. Check the LOM docs for your version.
- The browser loading commands are approximate — they open the browser but you may need to select the item manually depending on your Ableton version.

---

## Server Issues

### "Address already in use" / port conflict

**Fix:** Something else is using port 8080 or 8081.
```bash
# Find what's using port 8080
sudo lsof -i :8080
# Kill it or use a different port in config.yaml
```

### "websockets version mismatch"

The server uses `websockets` library. If you get import errors:
```bash
pip install --upgrade websockets aiohttp pyyaml
```

### Server crashes on startup

Check the config file syntax:
```bash
python -c "import yaml; yaml.safe_load(open('server/config.yaml'))"
```
If that errors, your YAML is malformed.

---

## Python API Issues

### "cannot reach bridge at http://localhost:8081"

**Fix:** The bridge server isn't running, or it's on a different port.
```bash
# Check if it's running
curl http://localhost:8081/status
```

### "Ableton is not connected" (AbletonNotConnectedError)

**Fix:** The bridge server is running but no Ableton client is connected. Either:
- Open Ableton and click Connect in the M4L device
- Or run the mock client for testing: `python tests/mock_client.py`

### Command times out

**Symptom:** `TimeoutError: Command 'play' timed out after 10s`

**Causes & fixes:**
- The M4L device is connected but not responding. Check the Max console for errors.
- The command_timeout in config.yaml is too short. Increase it.
- The JS engine crashed. Try clicking Disconnect → Connect in the device.

---

## Testing & Debugging

### Run the test suite

```bash
cd /opt/data/workspace/hermes-ableton-bridge
uv run pytest tests/ -v
```
All 20 tests should pass.

### Use the mock client

If you don't have Ableton running, use the mock client to test the full pipeline:

```bash
# Terminal 1
python server/ws_server.py --config server/config.yaml

# Terminal 2
python tests/mock_client.py

# Terminal 3
python -m hermes.ableton_api status
python -m hermes.ableton_api play
python -m hermes.ableton_api get_state
```

### Check server logs

The server logs to stdout. Start it with DEBUG level for verbose output:
```bash
python server/ws_server.py --config server/config.local.yaml
# Or set log_level: "DEBUG" in config.yaml
```

### Check Max console

In Ableton: View → Max Console (or the console icon in the device). All JS engine logs appear here with `[hermes]` prefix.

### Verify the WebSocket is reachable

From your Windows machine, open PowerShell:
```powershell
Test-NetConnection -ComputerName YOUR_VPS_IP -Port 8080
```

### Verify the HTTP API is working

On the VPS:
```bash
curl http://localhost:8081/status
# → {"status": "ok", "ableton_connected": false, ...}
```

---

## Still stuck?

1. Check the [ARCHITECTURE.md](ARCHITECTURE.md) to understand the data flow
2. Run the test suite to verify the Python side works
3. Use the mock client to isolate issues to either the server or the M4L device
4. Check the Max console for JS errors
5. Check the server logs for connection/auth issues
