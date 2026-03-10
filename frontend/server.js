/**
 * Frontend server for Network Traffic Monitor
 *
 * - Serves the static web UI on http://localhost:3000
 * - Connects to the sniffer's TCP broadcast port (default 9000)
 * - Bridges incoming JSON lines from the sniffer to browser clients via WebSocket
 */

const http = require("http");
const net = require("net");
const path = require("path");
const express = require("express");
const { WebSocketServer } = require("ws");

const SNIFFER_HOST = process.env.SNIFFER_HOST || "127.0.0.1";
const SNIFFER_PORT = parseInt(process.env.SNIFFER_PORT || "9000", 10);
const WEB_PORT = parseInt(process.env.WEB_PORT || "3000", 10);

const app = express();
const httpServer = http.createServer(app);
const wss = new WebSocketServer({ server: httpServer });

// Serve static files from ./public
app.use(express.static(path.join(__dirname, "public")));

// --------------------------------------------------------------------------
// WebSocket – browser clients
// --------------------------------------------------------------------------

wss.on("connection", (ws, req) => {
  const ip = req.socket.remoteAddress;
  console.log(`[+] Browser connected: ${ip}`);
  ws.on("close", () => console.log(`[-] Browser disconnected: ${ip}`));
  ws.on("error", (err) => console.error(`[!] WS error: ${err.message}`));
});

function broadcastToClients(payload) {
  const msg = typeof payload === "string" ? payload : JSON.stringify(payload);
  for (const ws of wss.clients) {
    if (ws.readyState === 1 /* OPEN */) {
      ws.send(msg);
    }
  }
}

// --------------------------------------------------------------------------
// TCP client – connects to the sniffer
// --------------------------------------------------------------------------

function connectToSniffer() {
  let buffer = "";
  const client = new net.Socket();

  client.connect(SNIFFER_PORT, SNIFFER_HOST, () => {
    console.log(`[*] Connected to sniffer at ${SNIFFER_HOST}:${SNIFFER_PORT}`);
  });

  client.on("data", (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split("\n");
    buffer = lines.pop(); // keep last (potentially incomplete) line

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        JSON.parse(trimmed); // validate before forwarding
        broadcastToClients(trimmed);
      } catch (err) {
        console.error(`[!] JSON parse error: ${err.message} — line: ${trimmed.slice(0, 80)}`);
      }
    }
  });

  client.on("close", () => {
    console.log(`[*] Sniffer connection closed. Retrying in 3 s…`);
    setTimeout(connectToSniffer, 3000);
  });

  client.on("error", (err) => {
    console.error(`[!] Sniffer TCP error: ${err.message}`);
    client.destroy();
  });
}

connectToSniffer();

// --------------------------------------------------------------------------
// Start HTTP / WS server
// --------------------------------------------------------------------------

httpServer.listen(WEB_PORT, () => {
  console.log(`[*] Web UI available at http://localhost:${WEB_PORT}`);
});
