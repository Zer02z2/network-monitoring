/**
 * Frontend server for Network Traffic Monitor
 *
 * Port 3000 — Monitor dashboard  (public/monitor/)
 * Port 3001 — Art sketch         (public/art/)
 *
 * Both WebSocket servers receive the same JSON stream from the sniffer (TCP 9000).
 */

const http = require("http");
const net  = require("net");
const path = require("path");
const express = require("express");
const { WebSocketServer } = require("ws");

const SNIFFER_HOST   = process.env.SNIFFER_HOST  || "127.0.0.1";
const SNIFFER_PORT   = parseInt(process.env.SNIFFER_PORT  || "9000", 10);
const MONITOR_PORT   = parseInt(process.env.MONITOR_PORT  || "3000", 10);
const ART_PORT       = parseInt(process.env.ART_PORT      || "3001", 10);

// --------------------------------------------------------------------------
// Helper: create an HTTP + WebSocket server pair for a static folder
// --------------------------------------------------------------------------
const createFrontend = (staticDir, port, label) => {
  const app    = express();
  const server = http.createServer(app);
  const wss    = new WebSocketServer({ server });

  app.use(express.static(staticDir));

  wss.on("connection", (ws, req) => {
    const ip = req.socket.remoteAddress;
    console.log(`[+] [${label}] browser connected: ${ip}`);
    ws.on("close", () => console.log(`[-] [${label}] browser disconnected: ${ip}`));
    ws.on("error", err => console.error(`[!] [${label}] WS error: ${err.message}`));
  });

  server.listen(port, () =>
    console.log(`[*] [${label}] serving http://localhost:${port}`)
  );

  return wss;
};

const monitorWss = createFrontend(
  path.join(__dirname, "public", "monitor"),
  MONITOR_PORT,
  "monitor"
);

const artWss = createFrontend(
  path.join(__dirname, "public", "art"),
  ART_PORT,
  "art"
);

// --------------------------------------------------------------------------
// Broadcast to all clients of a given WebSocket server
// --------------------------------------------------------------------------
const broadcast = (wss, msg) => {
  for (const ws of wss.clients) {
    if (ws.readyState === 1 /* OPEN */) ws.send(msg);
  }
};

// --------------------------------------------------------------------------
// Single TCP client → sniffer; forwards to both WebSocket servers
// --------------------------------------------------------------------------
const connectToSniffer = () => {
  let buffer = "";
  const client = new net.Socket();

  client.connect(SNIFFER_PORT, SNIFFER_HOST, () =>
    console.log(`[*] Connected to sniffer at ${SNIFFER_HOST}:${SNIFFER_PORT}`)
  );

  client.on("data", chunk => {
    buffer += chunk.toString();
    const lines = buffer.split("\n");
    buffer = lines.pop(); // keep last incomplete line

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        JSON.parse(trimmed); // validate
        broadcast(monitorWss, trimmed);
        broadcast(artWss,     trimmed);
      } catch (err) {
        console.error(`[!] JSON parse error: ${err.message}`);
      }
    }
  });

  client.on("close", () => {
    console.log("[*] Sniffer connection closed. Retrying in 3 s…");
    setTimeout(connectToSniffer, 3000);
  });

  client.on("error", err => {
    console.error(`[!] Sniffer TCP error: ${err.message}`);
    client.destroy();
  });
};

connectToSniffer();
