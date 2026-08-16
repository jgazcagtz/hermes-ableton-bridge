# Max for Live Guide

This guide explains how Max for Live devices work and how to install the Hermes Bridge.

## What is Max for Live?

Max for Live (M4L) is a platform that lets you build custom devices, instruments, and effects for Ableton Live using Max/MSP visual programming. M4L devices have direct access to Ableton's Live Object Model (LOM) API, which means they can control every aspect of Live — tracks, clips, devices, transport, the browser, etc.

## How the Hermes Bridge device works

The Hermes Bridge device (.amxd file) is a Max for Live device that:

1. **Connects to the VPS** via WebSocket (using Node for Max, since Max's JS engine can't do networking)
2. **Receives JSON commands** from the Hermes agent through the WebSocket
3. **Executes them in Ableton** via the LiveAPI JavaScript object
4. **Reports state back** to the VPS periodically

### Components

The device is made up of several files:

| File | Purpose |
|------|---------|
| `hermes_bridge.amxd` | The device file you drag into Ableton (Max patcher JSON) |
| `hermes_bridge_inner.maxpat` | The inner patch referenced by the .amxd |
| `hermes_bridge.maxpat` | Standalone Max patch (for debugging outside Ableton) |
| `hermes_bridge.js` | JavaScript engine — handles Live API calls and command dispatch |
| `hermes_bridge_node.js` | Node for Max script — manages the WebSocket connection |
| `package.json` | npm dependencies (the `ws` package) |

### How the JS and Node communicate

Max's built-in `js` object can't open network connections. So we use **Node for Max** (`node` object in Max) to handle the WebSocket. The JS engine and Node script communicate through shared dictionaries:

- `ws_in` dict: Node writes incoming WebSocket messages here, then signals the JS engine
- `ws_out` dict: JS engine writes outgoing messages here, then signals Node to send

This avoids Max's issues with JSON strings containing spaces (Max treats spaces as message separators).

## Installation

### Step 1: Install Node.js on Windows

Node for Max requires Node.js installed on your system.

1. Download from https://nodejs.org (LTS version)
2. Run the installer
3. Verify: open Command Prompt, run `node --version`

### Step 2: Copy the device files

Copy the entire `max-for-live/` folder to one of these locations:

**Option A — User Library (recommended):**
```
C:\Users\<your-username>\Documents\Ableton\User Library\Presets\Max for Live\Hermes Bridge\
```

**Option B — Any folder:**
Just remember where you put it. You can drag the `.amxd` file from any location.

### Step 3: Install the `ws` npm package

Node for Max needs the `ws` package for WebSocket support.

**Method 1 — In the device folder:**
1. Open Command Prompt
2. Navigate to the folder where you copied `max-for-live/`
3. Run: `npm install`

**Method 2 — In the Node for Max global folder:**
1. In Ableton: Options → Preferences → look for Max for Live / Node settings
2. Find the Node for Max package directory
3. Open Command Prompt there and run: `npm install ws`

### Step 4: Load the device in Ableton

1. Open Ableton Live
2. Open a project
3. Do one of:
   - **From the browser**: navigate to Max for Live → your library → Hermes Bridge → drag `hermes_bridge.amxd` onto a MIDI track
   - **From File Explorer**: drag `hermes_bridge.amxd` directly onto a MIDI track
4. The device appears in the track's device chain

### Step 5: Configure

1. In the device, find the **config** dict (labeled "Config dict — edit vps_host + auth_token")
2. Double-click to open the dict editor
3. Set:
   - `vps_host`: your VPS public IP (e.g., `82.123.45.67`)
   - `vps_port`: 8080
   - `auth_token`: the same token as in `server/config.yaml`
   - `use_ssl`: 0 (set to 1 if you enabled SSL on the server)
4. Close the dict editor

### Step 6: Connect

1. Click the **Connect** button in the device
2. Open the Max console (View → Max Console, or click the console button in the device)
3. You should see:
   ```
   [hermes] config loaded: 82.123.45.67:8080
   [hermes] connecting to ws://82.123.45.67:8080 ...
   [hermes] socket open — authenticating
   [hermes] authenticated
   ```

If you see "authenticated" — you're connected!

## How Max for Live devices work (general info)

### The .amxd file format

`.amxd` files are JSON files containing a Max patcher. The outer patcher wraps an inner patch (referenced by filename) inside a `live.device` object. The inner patch contains the actual logic.

You can open `.amxd` files in a text editor — they're just JSON. But it's easier to edit them in Max's visual editor (double-click the device in Ableton to open Max).

### The Live Object Model (LOM)

The LOM is Ableton's internal API. In JavaScript (via the `LiveAPI` object), you access it like this:

```javascript
var api = new LiveAPI("live_set");           // root
api.set("tempo", 120);                        // set tempo
api.call("start_play");                        // start playback
var numTracks = api.getcount("tracks");        // count tracks
var track = new LiveAPI("live_set tracks 0");  // access track 0
track.set("volume", -6.0);                    // set volume
var clip = new LiveAPI("live_set tracks 0 clip_slots 0 clip");
clip.call("set_notes");                       // enter note edit mode
clip.call("add_notes", 60, 0, 0.5, 100, 0);  // add a note
clip.call("notes");                           // exit note edit mode
```

Full LOM reference: https://docs.cycling74.com/max8/vignettes/live_object_model

### Node for Max

Node for Max lets you run Node.js scripts inside a Max patch. It's used for anything Max's built-in JS can't do: networking, file I/O, npm packages.

Communication between Max and Node:
- `Max.outlet("event", data)` — Node sends messages to Max
- `Max.addHandler("name", fn)` — Node receives messages from Max
- `Max.setDict("name", obj)` / `Max.getDict("name")` — shared dictionaries

## Editing the device

To edit the Max patch:
1. In Ableton, double-click the device title bar (or click the edit/wrench icon)
2. Max opens with the device patch
3. You can modify the layout, add objects, change connections
4. Save the patch (File → Save) — changes are live

## Tips

- The Max console (View → Max Console) is your best debugging tool. All log messages from the JS engine appear there.
- If the device doesn't load, check that all files (`hermes_bridge.js`, `hermes_bridge_node.js`, `hermes_bridge_inner.maxpat`) are in the same folder as `hermes_bridge.amxd`.
- Node for Max can be finicky. If you get "module not found" for `ws`, make sure you ran `npm install ws` in the right directory.
- The device works on any track type (MIDI, audio). It doesn't need to be on the track you're controlling — it controls the entire Live set.
- You only need ONE instance of the device. It controls all tracks.
