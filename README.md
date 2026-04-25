# Network Monitoring

A three-component system for capturing live network traffic and visualizing it — both on a browser canvas and a physical RGB LED matrix.

```
MacBook (Dev Machine)                          Raspberry Pi (Remote)
┌──────────────────────────────┐               ┌──────────────────────┐
│  sniffer/sniffer.py          │               │  matrix/matrix.py    │
│  Captures packets via tshark │──tunnel.py───▶│  Renders to RGB      │
│  Broadcasts JSON on TCP 9000 │               │  LED matrix hardware │
└──────────────┬───────────────┘               └──────────────────────┘
               │
               ▼
┌──────────────────────────────┐
│  frontend/server.js          │
│  Relays stream to browsers   │
│  Port 3000 → monitor table   │
│  Port 3001 → art canvas      │
└──────────────────────────────┘
```

---

## Components

### 1. Sniffer (`sniffer/`)

The sniffer captures TCP and UDP packets on a local network interface using **pyshark**, a Python wrapper around [tshark](https://www.wireshark.org/docs/man-pages/tshark.html) (Wireshark's CLI). It discovers IPs for target domains by watching TLS SNI fields and DNS responses, then tags and broadcasts each packet as a line-delimited JSON event over TCP.

Two modes:
- **Named domains** — filters traffic to a specific list of domain names
- **All traffic** (`--all`) — broadcasts every TCP/UDP packet without filtering

A separate `tunnel.py` script forwards the TCP stream from the sniffer to a remote host (e.g., Raspberry Pi running the matrix renderer).

### 2. Display Server (`matrix/`)

A Python renderer that listens for the sniffer's JSON stream and drives a physical **RGB LED matrix** via the [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) library. Two animation modes:

- **Normal** — Rectangles cluster around a Y position that drifts based on cumulative byte count. Red = outgoing, Blue = incoming.
- **Cascade** — An invisible sweep line travels top-to-bottom (incoming) or bottom-to-top (outgoing), spawning rectangles sequentially along its path.

Designed to run on a Raspberry Pi with an LED matrix panel connected via GPIO.

### 3. Web Client (`frontend/`)

A Node.js/Express server that connects once to the sniffer's TCP stream and fans out to all connected browser clients over WebSocket. Serves two separate UIs:

- **Monitor** (`localhost:3000`) — A live scrolling table of every captured packet: type, timestamp, protocol, source, destination, and byte count.
- **Art** (`localhost:3001`) — A fullscreen canvas animation that mirrors the matrix display logic in JavaScript, useful for developing and debugging animations without the physical hardware.

---

## Installation & Running

### Prerequisites

| Tool | Required by | Install |
|------|-------------|---------|
| Python 3.9+ | Sniffer, Matrix | [python.org](https://www.python.org/downloads/) |
| tshark (Wireshark CLI) | Sniffer | `brew install wireshark` |
| Node.js 18+ | Web client | [nodejs.org](https://nodejs.org/) |
| npm | Web client | Bundled with Node.js |

---

### Sniffer

**Requirements:** `sniffer/requirements.txt`
```
pyshark
```

**Setup:**
```bash
cd sniffer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Run — watch specific domains:**
```bash
python sniffer.py --names openai.com api.openai.com --interface en0
```

**Run — capture all TCP/UDP traffic:**
```bash
python sniffer.py --all --interface en0
```

**Full options:**
```
--names DOMAIN ...     Domains to monitor via TLS SNI and DNS
--all                  Capture all TCP/UDP instead of filtering by domain
--interface IFACE      Network interface to sniff (default: en0)
--port PORT            TCP broadcast port (default: 9000)
--send-interval MS     Milliseconds between event broadcasts (default: 1)
```

> **Note:** tshark must be installed and accessible on your PATH. On macOS, check `which tshark` after installing Wireshark. You may need to add `/Applications/Wireshark.app/Contents/MacOS` to your PATH or use `brew install wireshark`.

**Forward to a remote host (e.g., Raspberry Pi):**
```bash
python tunnel.py --target-host raspberrypi.local --target-port 9001 --sniffer-port 9000
```

---

### Display Server (Raspberry Pi)

**Requirements** (install on the Raspberry Pi):
```
numpy
Pillow
rgbmatrix
```

**Setup:**
```bash
# Standard dependencies
pip3 install numpy Pillow

# RGB matrix driver — must be built from source on the Pi
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git
cd rpi-rgb-led-matrix/bindings/python
pip3 install -r requirements.txt
pip3 install .
```

**Run:**
```bash
sudo python3 matrix.py --port 9001
```

> `sudo` is required for GPIO access on Raspberry Pi.

**Full options:**
```
--port PORT                  TCP listen port for sniffer stream (default: 9001)
--mode {NORMAL,CASCADE}      Animation style (default: NORMAL)
--led-rows N                 Matrix height in pixels (default: 64)
--led-cols N                 Matrix width in pixels (default: 64)
--led-chain N                Number of panels daisy-chained horizontally (default: 1)
--led-parallel N             Number of panel rows in parallel (default: 1)
--led-brightness 0-100       Brightness level (default: 100)
--led-pwm-bits N             PWM bit depth for color (default: 7)
--led-slowdown-gpio N        GPIO slowdown for faster Pi models (default: 1)
--led-hardware-mapping STR   Hardware pinout variant (default: regular)
--led-show-refresh           Show refresh rate on the display
```

**Example for a 3×3 chain of 64×64 panels:**
```bash
sudo python3 matrix.py \
  --port 9001 \
  --mode CASCADE \
  --led-chain 3 \
  --led-parallel 3 \
  --led-rows 64 \
  --led-cols 64 \
  --led-pwm-bits 7 \
  --led-slowdown-gpio 3
```

---

### Web Client

**Requirements:** `frontend/package.json`
```json
{
  "express": "^4.18.2",
  "ws": "^8.16.0"
}
```

**Setup:**
```bash
cd frontend
npm install
```

**Run:**
```bash
npm start
```

**With custom sniffer location:**
```bash
SNIFFER_HOST=192.168.1.42 SNIFFER_PORT=9000 npm start
```

**Environment variables:**
```
SNIFFER_HOST     Host running the sniffer (default: 127.0.0.1)
SNIFFER_PORT     Sniffer TCP port (default: 9000)
MONITOR_PORT     Port for the monitor table UI (default: 3000)
ART_PORT         Port for the art canvas UI (default: 3001)
```

**Open in browser:**
- Monitor table: http://localhost:3000
- Art canvas: http://localhost:3001

---

## Quick Start (local dev, no hardware)

Open three terminals:

**Terminal 1 — Sniffer:**
```bash
cd sniffer
source venv/bin/activate
python sniffer.py --names openai.com --interface en0
```

**Terminal 2 — Web client:**
```bash
cd frontend
npm start
```

**Terminal 3 — Open browsers:**
```
http://localhost:3000   ← live packet table
http://localhost:3001   ← art canvas preview
```

Generate traffic (e.g., curl openai.com) and watch packets appear in real time.
