/**
 * hermes_bridge.js
 * =================
 * JavaScript engine for the Hermes Bridge Max for Live device.
 *
 * Runs inside the Max for Live "js" object. Responsibilities:
 *   - Manage the WebSocket client connection lifecycle (connect/auth/reconnect/backoff)
 *   - Receive JSON command messages from the VPS bridge server
 *   - Dispatch commands to the Live Object Model via the LiveAPI object
 *   - Build JSON state reports and send them back through the WebSocket
 *   - Send JSON response messages (ok/error) for every command
 *   - Log to the Max window for debugging
 *
 * This script is loaded by the `js hermes_bridge.js` object in hermes_bridge.amxd.
 * It reads configuration from the [dict config] object (vps_host, vps_port, auth_token).
 *
 * Live API notes
 * --------------
 * Max for Live exposes the Live Object Model through the LiveAPI JavaScript object.
 * Example:
 *     var api = new LiveAPI("live_set");
 *     api.set("tempo", 120);          // set a property
 *     api.call("start_play");          // call a function
 *     var tracks = api.getcount("tracks");  // count children
 *     var t = new LiveAPI("live_set tracks " + i);
 *
 * Full Live API docs: https://docs.cycling74.com/ and Ableton's Max for Live
 * Live Object Model reference.
 */

// -------------------------------------------------------------------- //
//  Globals
// -------------------------------------------------------------------- //
inlets = 1;
outlets = 2; // outlet 0: JSON strings to websocket send object
            // outlet 1: status/log strings (to message box / print)

var WS = null;          // WebSocket handle (from Max's [node ws] or the built-in socket)
var connected = false;
var authed = false;
var reconnectDelay = 1;  // seconds, exponential backoff
var reconnectTask = null;

// Config — populated from the [dict config] object via "load_config" message.
var CONFIG = {
    vps_host: "127.0.0.1",
    vps_port: 8080,
    auth_token: "change-me-please",
    use_ssl: false,
    state_interval: 2.0
};

var stateTask = null;

// -------------------------------------------------------------------- //
//  Logging
// -------------------------------------------------------------------- //
function log(msg) {
    outlet(1, "[hermes] " + msg);
    post("[hermes] " + msg + "\n");
}

// -------------------------------------------------------------------- //
//  Configuration (called from Max when dict config changes)
// -------------------------------------------------------------------- //
function load_config(dictName) {
    var d = new Dict(dictName);
    CONFIG.vps_host = (d.get("vps_host") != null) ? d.get("vps_host") : "127.0.0.1";
    CONFIG.vps_port = (d.get("vps_port") != null) ? d.get("vps_port") : 8080;
    CONFIG.auth_token = (d.get("auth_token") != null) ? d.get("auth_token") : "change-me-please";
    CONFIG.use_ssl = (d.get("use_ssl") == 1);
    CONFIG.state_interval = (d.get("state_interval") != null) ? d.get("state_interval") : 2.0;
    log("config loaded: " + CONFIG.vps_host + ":" + CONFIG.vps_port);
}

// -------------------------------------------------------------------- //
//  Connection lifecycle
//  The `node` object (hermes_bridge_node.js) owns the socket. It writes
//  incoming messages to the `ws_in` dict and bangs "cmd". We write outgoing
//  payloads to the `ws_out` dict and bang "send" back to the node object.
// -------------------------------------------------------------------- //
function connect() {
    if (connected) return;
    var scheme = CONFIG.use_ssl ? "wss" : "ws";
    var url = scheme + "://" + CONFIG.vps_host + ":" + CONFIG.vps_port;
    log("connecting to " + url + " ...");
    outlet(0, "connect", url);  // tell the node object to open the socket
}

// Called by the node object ("open") when the socket opens.
function open() {
    connected = true;
    reconnectDelay = 1;
    log("socket open — authenticating");
    var authObj = {auth: CONFIG.auth_token};
    setOutDict(authObj);
    outlet(0, "send");
}

// Called by the node object ("cmd") when a message arrived in dict ws_in.
function cmd() {
    var d = new Dict("ws_in");
    var raw = d.stringify();
    var msg = JSON.parse(raw);
    var mtype = msg.type;
    if (mtype == "auth") {
        if (msg.status == "ok") {
            authed = true;
            log("authenticated");
            startStateReports();
        } else {
            log("auth FAILED: " + msg.error);
            authed = false;
            disconnect();
        }
        return;
    }
    if (mtype == "command") {
        handleCommand(msg);
        return;
    }
    // ignore unknown
}

// Called by the node object ("close").
function close() {
    connected = false;
    authed = false;
    stopStateReports();
    log("socket closed — scheduling reconnect in " + reconnectDelay + "s");
    if (reconnectTask) clearTimeout(reconnectTask);
    reconnectTask = setTimeout(function () { connect(); }, reconnectDelay * 1000);
    reconnectDelay = Math.min(30, reconnectDelay * 2);
}

// Called by the node object ("error").
function error() { log("socket error: " + Array.prototype.slice.call(arguments).join(" ")); }
// Called by the node object ("log").
function logmsg() { log(Array.prototype.slice.call(arguments).join(" ")); }

function disconnect() {
    if (reconnectTask) { clearTimeout(reconnectTask); reconnectTask = null; }
    outlet(0, "close");
    connected = false;
    authed = false;
    stopStateReports();
}

// Write a JS object to the shared `ws_out` dict (Max parses it back to JSON).
function setOutDict(obj) {
    var d = new Dict("ws_out");
    d.clear();
    d.parse(JSON.stringify(obj));
}

function sendObj(obj) {
    setOutDict(obj);
    outlet(0, "send");  // tell node to read ws_out and send it
}

// -------------------------------------------------------------------- //
//  Command dispatch
// -------------------------------------------------------------------- //
function handleCommand(msg) {
    var id = msg.id;
    var action = msg.action;
    var params = msg.params || {};
    var resp = {id: id, type: "response", status: "ok", data: {}, error: null, timestamp: Math.floor(Date.now() / 1000)};
    try {
        resp.data = execute(action, params);
    } catch (e) {
        resp.status = "error";
        resp.error = String(e);
        log("command error [" + action + "]: " + e);
    }
    sendObj(resp);
}

function execute(action, p) {
    switch (action) {
        // ---- Transport ----
        case "play": {
            var api = new LiveAPI("live_set");
            api.call("start_play");
            return {playing: true};
        }
        case "stop": {
            var api = new LiveAPI("live_set");
            api.call("stop_play");
            return {playing: false};
        }
        case "set_tempo": return setTempo(p.tempo);
        case "set_time_signature": return setTimeSignature(p.numerator, p.denominator);
        case "toggle_loop": return toggleProp("live_set", "loop");
        case "toggle_metronome": return toggleProp("live_set", "metronome");
        case "overdub": {
            var api = new LiveAPI("live_set");
            var cur = api.get("overdub");
            var on = (p.on != null) ? !!p.on : !cur;
            api.set("overdub", on ? 1 : 0);
            return {overdub: on};
        }
        // ---- Tracks ----
        case "create_midi_track": return createTrack("midi", p.index);
        case "create_audio_track": return createTrack("audio", p.index);
        case "delete_track": return deleteTrack(p.index);
        case "duplicate_track": return duplicateTrack(p.index);
        case "mute_track": return setTrackProp(p.index, "mute", !!p.mute);
        case "solo_track": return setTrackProp(p.index, "solo", !!p.solo);
        case "set_volume": return setTrackProp(p.index, "volume", p.volume);
        case "set_pan": return setTrackProp(p.index, "pan", p.pan);
        case "set_send": return setSend(p.index, p.send, p.value);
        // ---- Clips ----
        case "create_midi_clip": return createMidiClip(p.track, p.length_beats, p.scene);
        case "set_clip_length": return setClipLength(p.track, p.clip, p.length_beats);
        case "add_note": return addNote(p.track, p.clip, p.pitch, p.start, p.duration, p.velocity);
        case "add_notes": return addNotes(p.track, p.clip, p.notes);
        case "remove_note": return removeNote(p.track, p.clip, p.pitch, p.start);
        case "clear_clip": return clearClip(p.track, p.clip);
        case "quantize_clip": return quantizeClip(p.track, p.clip, p.grid);
        case "toggle_clip_loop": return toggleClipLoop(p.track, p.clip);
        // ---- Browser ----
        case "load_instrument": return loadBrowser(p.track, p.name, "instrument");
        case "load_effect": return loadBrowser(p.track, p.name, "audio_effect");
        case "load_sample": return loadBrowser(p.track, p.name, "sample");
        case "load_drum_rack": return loadBrowser(p.track, "Drum Rack", "drum_rack");
        // ---- Devices ----
        case "set_device_parameter": return setDeviceParameter(p.track, p.device, p.param, p.value);
        case "get_device_parameters": return getDeviceParameters(p.track, p.device);
        // ---- Scenes ----
        case "create_scene": return createScene(p.name);
        case "launch_scene": return launchScene(p.scene);
        case "reorder_scene": return reorderScene(p.scene, p.new_index);
        // ---- State ----
        case "get_full_state": return getFullState();
        default:
            throw "unknown action: " + action;
    }
}

// -------------------------------------------------------------------- //
//  Live API command implementations
// -------------------------------------------------------------------- //
function liveSet() { return new LiveAPI("live_set"); }

function setTempo(bpm) {
    var api = liveSet();
    api.set("tempo", bpm);
    return {tempo: bpm};
}

function setTimeSignature(num, den) {
    var api = liveSet();
    api.set("signature_numerator", num);
    api.set("signature_denominator", den);
    return {time_signature: [num, den]};
}

function toggleProp(path, prop) {
    var api = new LiveAPI(path);
    var cur = api.get(prop);
    api.set(prop, cur ? 0 : 1);
    var obj = {}; obj[prop] = !cur;
    return obj;
}

function trackPath(index) { return "live_set tracks " + index; }

function createTrack(kind, index) {
    var api = liveSet();
    var count = api.getcount("tracks");
    var insertAt = (index != null) ? index : count;
    if (kind == "midi") {
        api.call("create_midi_track", insertAt);
    } else {
        api.call("create_audio_track", insertAt);
    }
    return {track: insertAt, count: count + 1};
}

function deleteTrack(index) {
    var api = liveSet();
    api.call("delete_track", index);
    return {deleted: index};
}

function duplicateTrack(index) {
    var api = liveSet();
    api.call("duplicate_track", index);
    return {duplicated: index};
}

function setTrackProp(index, prop, value) {
    var api = new LiveAPI(trackPath(index));
    api.set(prop, value);
    var obj = {track: index}; obj[prop] = value;
    return obj;
}

function setSend(index, send, value) {
    // sends are children of a track
    var api = new LiveAPI(trackPath(index) + " chain_sends " + send);
    // Fallback: try mixing_track_sends / track_sends
    api.set("value", value);
    return {track: index, send: send, value: value};
}

function createMidiClip(track, lengthBeats, scene) {
    var t = new LiveAPI(trackPath(track));
    var numClips = t.getcount("clip_slots");
    var slot = (scene != null) ? scene : 0;
    var cs = new LiveAPI(trackPath(track) + " clip_slots " + slot);
    cs.call("create_clip");
    // set the clip length
    if (lengthBeats != null) {
        var clip = new LiveAPI(trackPath(track) + " clip_slots " + slot + " clip");
        clip.set("loop_end", lengthBeats);
    }
    return {track: track, clip: slot, length_beats: lengthBeats};
}

function setClipLength(track, clip, lengthBeats) {
    var c = new LiveAPI(trackPath(track) + " clip_slots " + clip + " clip");
    c.set("loop_end", lengthBeats);
    return {length_beats: lengthBeats};
}

function addNote(track, clip, pitch, start, duration, velocity) {
    var c = new LiveAPI(trackPath(track) + " clip_slots " + clip + " clip");
    c.call("set_notes");
    c.call("add_notes", pitch, start, duration, velocity, 0);
    c.call("notes");
    return {added: true};
}

function addNotes(track, clip, notes) {
    var c = new LiveAPI(trackPath(track) + " clip_slots " + clip + " clip");
    c.call("set_notes");
    for (var i = 0; i < notes.length; i++) {
        var n = notes[i];
        c.call("add_notes", n.pitch, n.start, n.duration, n.velocity, 0);
    }
    c.call("notes");
    return {added: notes.length};
}

function removeNote(track, clip, pitch, start) {
    var c = new LiveAPI(trackPath(track) + " clip_slots " + clip + " clip");
    c.call("remove_notes", start, pitch, start + 0.001, pitch + 1);
    return {removed: true};
}

function clearClip(track, clip) {
    var c = new LiveAPI(trackPath(track) + " clip_slots " + clip + " clip");
    c.call("remove_all_notes");
    return {cleared: true};
}

function quantizeClip(track, clip, grid) {
    var c = new LiveAPI(trackPath(track) + " clip_slots " + clip + " clip");
    var g = grid || 4;
    c.call("quantize", g);
    return {quantized: true};
}

function toggleClipLoop(track, clip) {
    var c = new LiveAPI(trackPath(track) + " clip_slots " + clip + " clip");
    var cur = c.get("looping");
    c.set("looping", cur ? 0 : 1);
    return {loop: !cur};
}

// Browser loading: the browser API is limited; we drive it via the track's
// `load_device` / `load_instrument_or_sample` calls where possible. Searching
// by name requires iterating the browser tree, which we approximate here.
function loadBrowser(track, name, kind) {
    // Try the clip-slot / track based loaders.
    var t = new LiveAPI(trackPath(track));
    try {
        if (kind == "instrument") {
            t.call("load_instrument_or_sample_browser");
        } else if (kind == "audio_effect") {
            t.call("load_audio_effect_browser");
        } else if (kind == "sample") {
            t.call("load_instrument_or_sample_browser");
        } else {
            t.call("load_device_browser");
        }
        log("opened browser for '" + name + "' (load from browser window)");
        return {loaded: name, note: "browser opened — select item to load"};
    } catch (e) {
        throw "browser load failed: " + e;
    }
}

function setDeviceParameter(track, device, param, value) {
    var d = new LiveAPI(trackPath(track) + " devices " + device + " parameters " + param);
    d.set("value", value);
    return {value: value};
}

function getDeviceParameters(track, device) {
    var d = new LiveAPI(trackPath(track) + " devices " + device);
    var count = d.getcount("parameters");
    var params = [];
    for (var i = 0; i < count; i++) {
        var p = new LiveAPI(trackPath(track) + " devices " + device + " parameters " + i);
        params.push({name: p.get("name"), value: p.get("value")});
    }
    return {parameters: params};
}

function createScene(name) {
    var api = liveSet();
    var count = api.getcount("scenes");
    api.call("create_scene", count);
    if (name) {
        var s = new LiveAPI("live_set scenes " + count);
        s.set("name", name);
    }
    return {scene: count};
}

function launchScene(scene) {
    var s = new LiveAPI("live_set scenes " + scene);
    s.call("launch");
    return {launched: scene};
}

function reorderScene(scene, newIndex) {
    var api = liveSet();
    api.call("move_scene", scene, newIndex);
    return {reordered: true};
}

// -------------------------------------------------------------------- //
//  State reporting
// -------------------------------------------------------------------- //
function startStateReports() {
    stopStateReports();
    var intervalMs = (CONFIG.state_interval || 2.0) * 1000;
    stateTask = setInterval(function () {
        if (!authed) return;
        sendObj({type: "state", data: getFullState(), timestamp: Math.floor(Date.now() / 1000)});
    }, intervalMs);
}

function stopStateReports() {
    if (stateTask) { clearInterval(stateTask); stateTask = null; }
}

function getFullState() {
    var api = liveSet();
    var state = {
        tempo: api.get("tempo"),
        playing: api.get("is_playing") ? true : false,
        loop: api.get("loop") ? true : false,
        metronome: api.get("metronome") ? true : false,
        overdub: api.get("overdub") ? true : false,
        time_signature: [api.get("signature_numerator"), api.get("signature_denominator")],
        tracks: [],
        scenes: []
    };
    var nTracks = api.getcount("tracks");
    for (var i = 0; i < nTracks; i++) {
        var t = new LiveAPI(trackPath(i));
        var track = {
            index: i,
            name: t.get("name"),
            type: t.get("can_have_audio_out") ? "audio" : "midi",
            volume: t.get("volume"),
            pan: t.get("pan"),
            mute: t.get("mute") ? true : false,
            solo: t.get("solo") ? true : false,
            clips: [],
            devices: []
        };
        var nClips = t.getcount("clip_slots");
        for (var c = 0; c < nClips; c++) {
            var cs = new LiveAPI(trackPath(i) + " clip_slots " + c);
            var hasClip = cs.get("has_clip");
            if (hasClip) {
                var clip = new LiveAPI(trackPath(i) + " clip_slots " + c + " clip");
                track.clips.push({
                    index: c,
                    length_beats: clip.get("loop_end"),
                    loop: clip.get("looping") ? true : false,
                    notes: []  // omitting note contents for brevity; query per-clip if needed
                });
            }
        }
        var nDev = t.getcount("devices");
        for (var d = 0; d < nDev; d++) {
            var dev = new LiveAPI(trackPath(i) + " devices " + d);
            track.devices.push({index: d, name: dev.get("name"),
                                 type: dev.get("type"), parameters: []});
        }
        state.tracks.push(track);
    }
    var nScenes = api.getcount("scenes");
    for (var s = 0; s < nScenes; s++) {
        var sc = new LiveAPI("live_set scenes " + s);
        state.scenes.push({index: s, name: sc.get("name")});
    }
    return state;
}

// Expose for Max message routing.
// (Max calls functions by name when a matching message arrives at the inlet.)
// We also support a generic "json_in" inlet for the websocket object's outlet.
function json_in(raw) { on_message(raw); }
