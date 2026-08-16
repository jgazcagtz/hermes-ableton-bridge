/**
 * hermes_bridge_node.js
 * =====================
 * Node for Max WebSocket client used by the Hermes Bridge device.
 *
 * Max's built-in `js` object has no network sockets, so the device uses a
 * `node` object (Node for Max) running this script. It owns the WebSocket
 * connection (auth + reconnect/backoff) and exchanges parsed JSON objects
 * with Max through two shared dicts — avoiding Max's symbol/space problems
 * with raw JSON strings:
 *
 *   ws_in   : dict that THIS script writes with each incoming ws message
 *             (parsed to a JS object via Max.setDict), then bangs outlet 0
 *             with "cmd" so the js object reads it.
 *   ws_out  : dict that the js object writes with the response/state to send;
 *             THIS script reads it via Max.getDict when it receives "send".
 *
 * Messages handled (inlet 0, from Max):
 *   "connect <url>"   open the WebSocket to <url>
 *   "send"            read dict ws_out and send it as JSON over the socket
 *   "close"           close the socket and stop reconnecting
 *
 * Messages emitted (outlet 0, to Max):
 *   "open"            socket opened
 *   "cmd"             a message arrived (read dict ws_in)
 *   "close"           socket closed
 *   "error <msg>"     socket error
 *   "log <msg>"       status logging
 *   "connecting"       attempting connection
 *
 * Requires the `ws` npm package in the Node for Max environment:
 *     Max → Node for Max menu → "Open Package Folder"  →  npm install ws
 */

const Max = require("max-api");
const WebSocket = require("ws");

let ws = null;
let reconnectDelay = 1000;
let wantConnect = false;
let reconnectTimer = null;
let currentUrl = null;

function doConnect(url) {
    wantConnect = true;
    currentUrl = url;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }
    Max.outlet("connecting");
    try {
        ws = new WebSocket(url);
    } catch (e) {
        Max.outlet("error", String(e));
        scheduleReconnect();
        return;
    }
    ws.on("open", () => {
        reconnectDelay = 1000;
        Max.outlet("open");
    });
    ws.on("message", (data) => {
        const str = data.toString();
        let obj;
        try {
            obj = JSON.parse(str);
        } catch (e) {
            Max.outlet("log", "non-JSON ws message ignored");
            return;
        }
        Max.setDict("ws_in", obj);
        Max.outlet("cmd");
    });
    ws.on("close", () => {
        Max.outlet("close");
        if (wantConnect) scheduleReconnect();
    });
    ws.on("error", (err) => {
        Max.outlet("error", String(err));
    });
}

function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    Max.outlet("log", "reconnect in " + (reconnectDelay / 1000) + "s");
    reconnectTimer = setTimeout(() => { if (wantConnect && currentUrl) doConnect(currentUrl); }, reconnectDelay);
    reconnectDelay = Math.min(30000, reconnectDelay * 2);
}

async function doSend() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
        const obj = await Max.getDict("ws_out");
        if (obj && Object.keys(obj).length) {
            ws.send(JSON.stringify(obj));
        }
    } catch (e) {
        Max.outlet("error", "send failed: " + e);
    }
}

function doClose() {
    wantConnect = false;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
}

Max.addHandler("connect", (url) => doConnect(url));
Max.addHandler("send", () => doSend());
Max.addHandler("close", () => doClose());
Max.addHandler("bang", () => {}); // keep inlet alive
