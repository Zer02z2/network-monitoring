#!/usr/bin/env python3
"""
RGB Matrix renderer for Network Traffic Monitor.

Connects to the sniffer TCP stream and renders the same art visualization
as the browser canvas onto a physical RGB LED matrix.

Usage:
    sudo python3 matrix.py -ip 127.0.0.1 -port 9000 \
        --led-chain=3 --led-parallel=3 --led-rows=64 --led-cols=64 \
        --led-pwm-bits=7 --led-pwm-dither-bits=1 \
        --led-slowdown-gpio=3 --led-pwm-lsb-nanoseconds=50 \
        --led-show-refresh
"""

import argparse
import json
import math
import queue
import random
import socket
import threading
import time

from rgbmatrix import RGBMatrix, RGBMatrixOptions

# ── TUNABLE CONFIG ────────────────────────────────────────────────────────
BYTES_PER_GRID    = 20    # bytes per grid cell (ceil)
DISAPPEAR_DELAY   = 100   # ms — pause before a packet's cells start fading
DISAPPEAR_STEP    = 5     # ms — time between each cell erasing
FLASH_THRESHOLD   = 300   # bytes — minimum size to trigger white flash bursts
FLASH_BURST_BYTES = 100   # bytes per burst (decoupled from BYTES_PER_GRID)
FLASH_SCALE_BYTES = 200   # bytes per interval — adds 1 flash grid & round each
FLASH_ROUND_DELAY = 10    # ms — gap between bursts
FLASH_DURATION    = 50    # ms — how long a pixel stays white
FLASH_QUEUE_CAP   = 100   # max entries in flash queue at any time
CYAN_CHANCE       = 0.15  # probability a new grid is cyan vs pink
TARGET_FPS        = 60

COLOR_BG    = (0,   0,   0)
COLOR_PINK  = (255, 45,  111)
COLOR_CYAN  = (0,   255, 224)
COLOR_WHITE = (255, 255, 255)
# ─────────────────────────────────────────────────────────────────────────


def now_ms() -> float:
    return time.monotonic() * 1000


# ── Framebuffer ───────────────────────────────────────────────────────────
# Tracks current pixel state so both hardware double-buffers stay in sync.
# Only stores non-black pixels. Dirty set accumulates all changes each frame.
class Framebuffer:
    def __init__(self):
        self._pixels: dict[tuple[int, int], tuple[int, int, int]] = {}
        self._dirty:  set[tuple[int, int]] = set()

    def set(self, col: int, row: int, color: tuple[int, int, int]):
        if color == COLOR_BG:
            self._pixels.pop((col, row), None)
        else:
            self._pixels[(col, row)] = color
        self._dirty.add((col, row))

    def flush(self, canvas) -> set[tuple[int, int]]:
        """Apply dirty pixels to canvas. Returns the set of coords flushed."""
        flushed = set(self._dirty)
        for (col, row) in flushed:
            r, g, b = self._pixels.get((col, row), COLOR_BG)
            canvas.SetPixel(col, row, r, g, b)
        self._dirty.clear()
        return flushed

    def clear(self):
        self._pixels.clear()
        self._dirty.clear()


fb = Framebuffer()


# ── Grid ──────────────────────────────────────────────────────────────────
class Grid:
    def __init__(self, col: int, rel_row: int, color: tuple, packet):
        self.col         = col
        self.rel_row     = rel_row
        self.color       = color   # mutable — swap_one_color changes this live
        self.packet      = packet  # back-ref so abs_row is always current
        self.cleared     = False
        self.flash_until = 0.0    # ms timestamp; 0 = not flashing

    @property
    def abs_row(self) -> int:
        return self.packet.row_start + self.rel_row

    def draw(self):
        if not self.cleared:
            fb.set(self.col, self.abs_row, self.color)

    # Locked: ignored if already flashing (flash_until > 0)
    def flash(self):
        if self.cleared or self.flash_until > 0:
            return
        self.flash_until = now_ms() + FLASH_DURATION
        fb.set(self.col, self.abs_row, COLOR_WHITE)
        self.packet.swap_one_color()

    # Fade wins: clears flash state immediately without waiting
    def erase(self):
        self.flash_until = 0
        self.cleared     = True
        fb.set(self.col, self.abs_row, COLOR_BG)

    def stop_flash(self):
        self.flash_until = 0


# ── Packet ────────────────────────────────────────────────────────────────
class Packet:
    def __init__(self, bytes_: int, row_start: int, cols: int):
        self.row_start = row_start

        grid_count     = max(1, math.ceil(bytes_ / BYTES_PER_GRID))
        self.row_count = math.ceil(grid_count / cols)

        self.grids: list[Grid] = [
            Grid(
                i % cols,
                i // cols,
                COLOR_CYAN if random.random() < CYAN_CHANCE else COLOR_PINK,
                self,
            )
            for i in range(grid_count)
        ]

        self._fade_start_at = math.inf
        self._next_erase_at = math.inf
        self._fade_idx      = grid_count - 1
        self._done          = False

    def draw(self):
        for g in self.grids:
            g.draw()

    def start(self):
        self.draw()
        t = now_ms()
        self._fade_start_at = t + DISAPPEAR_DELAY
        self._next_erase_at = self._fade_start_at

    # Called every frame. Returns True when this packet is fully gone.
    def tick(self, now: float) -> bool:
        if self._done:
            return False

        # Restore grids whose flash duration has elapsed
        for g in self.grids:
            if not g.cleared and g.flash_until > 0 and now >= g.flash_until:
                g.flash_until = 0
                fb.set(g.col, g.abs_row, g.color)

        # Fade: erase one grid per DISAPPEAR_STEP after DISAPPEAR_DELAY
        if now < self._fade_start_at or now < self._next_erase_at:
            return False

        while self._fade_idx >= 0 and self.grids[self._fade_idx].cleared:
            self._fade_idx -= 1

        if self._fade_idx < 0:
            self._done = True
            return True  # signal PacketManager to call on_gone

        self.grids[self._fade_idx].erase()
        self._fade_idx      -= 1
        self._next_erase_at  = now + DISAPPEAR_STEP
        return False

    def stop_timers(self):
        self._fade_start_at = math.inf
        self._next_erase_at = math.inf
        self._fade_idx      = -1
        self._done          = True
        for g in self.grids:
            g.stop_flash()

    def swap_one_color(self):
        live  = [g for g in self.grids if not g.cleared]
        cyans = [g for g in live if g.color == COLOR_CYAN]
        pinks = [g for g in live if g.color == COLOR_PINK]
        if not cyans or not pinks:
            return

        cg = random.choice(cyans)
        pg = random.choice(pinks)
        cg.color = COLOR_PINK
        pg.color = COLOR_CYAN

        if not cg.flash_until:
            cg.draw()
        if not pg.flash_until:
            pg.draw()

    # Push burst entries into the global flash queue
    def trigger_flash_bursts(self, burst_count: int, get_all_lit, flash_grids: int, flash_rounds: int):
        t = now_ms()
        for b in range(burst_count):
            for r in range(flash_rounds):
                if len(flash_queue) >= FLASH_QUEUE_CAP:
                    return
                flash_queue.append({
                    "at":          t + (b * flash_rounds + r) * FLASH_ROUND_DELAY,
                    "get_all_lit": get_all_lit,
                    "flash_grids": flash_grids,
                    "owner":       self,
                })


# ── Flash queue — drained by the main animation loop ─────────────────────
flash_queue: list[dict] = []


# ── PacketManager ─────────────────────────────────────────────────────────
class PacketManager:
    def __init__(self, cols: int, rows: int):
        self.cols           = cols
        self.rows           = rows
        self.packets:       list[Packet] = []
        self.next_row_start = 0

    def add(self, bytes_: int):
        new_row_count = math.ceil(max(1, math.ceil(bytes_ / BYTES_PER_GRID)) / self.cols)
        while self.packets and self.next_row_start + new_row_count > self.rows:
            self._evict_top()

        p = Packet(bytes_, self.next_row_start, self.cols)
        self.packets.append(p)
        self.next_row_start += p.row_count
        p.start()

        if bytes_ > FLASH_THRESHOLD:
            burst_count  = (bytes_ - FLASH_THRESHOLD) // FLASH_BURST_BYTES
            flash_grids  = max(1, bytes_ // FLASH_SCALE_BYTES)
            flash_rounds = max(1, bytes_ // FLASH_SCALE_BYTES)
            p.trigger_flash_bursts(burst_count, self._all_lit_grids, flash_grids, flash_rounds)

    def _all_lit_grids(self) -> list[Grid]:
        return [g for p in self.packets for g in p.grids if not g.cleared]

    def _purge_flash_queue(self, owner: Packet):
        for i in range(len(flash_queue) - 1, -1, -1):
            if flash_queue[i]["owner"] is owner:
                flash_queue.pop(i)

    def _shift_packets_up(self, from_idx: int, freed: int):
        """Erase old positions, shift row_start, redraw at new positions."""
        for p in self.packets[from_idx:]:
            for g in p.grids:
                if not g.cleared:
                    fb.set(g.col, g.abs_row, COLOR_BG)
            p.row_start -= freed
            for g in p.grids:
                if not g.cleared:
                    fb.set(g.col, g.abs_row, g.color)
        self.next_row_start -= freed

    def _evict_top(self):
        top = self.packets.pop(0)
        top.stop_timers()
        self._purge_flash_queue(top)
        self._shift_packets_up(0, top.row_count)
        for g in self._all_lit_grids():
            g.flash()

    def on_gone(self, packet: Packet):
        idx = self.packets.index(packet)
        self.packets.pop(idx)
        self._purge_flash_queue(packet)
        self._shift_packets_up(idx, packet.row_count)

    def clear(self):
        for p in self.packets:
            p.stop_timers()
        self.packets.clear()
        self.next_row_start = 0
        flash_queue.clear()
        fb.clear()


# ── TCP client — runs in background thread ────────────────────────────────
def tcp_client(host: str, port: int, incoming: queue.Queue):
    while True:
        try:
            with socket.create_connection((host, port)) as sock:
                print(f"[*] Connected to sniffer at {host}:{port}")
                buf = ""
                while True:
                    chunk = sock.recv(4096).decode("utf-8", errors="ignore")
                    if not chunk:
                        break
                    buf += chunk
                    lines = buf.split("\n")
                    buf = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            if data.get("type") in ("traffic", "new_ip"):
                                incoming.put(data.get("length", 0))
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            print(f"[!] Sniffer connection error: {e}. Retrying in 3 s...")
            time.sleep(3)


# ── Argument parsing ──────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="RGB Matrix Network Art")

    # Sniffer connection
    parser.add_argument("-ip",   default="127.0.0.1",
                        help="Sniffer host/mDNS name (default: 127.0.0.1)")
    parser.add_argument("-port", type=int, default=9000,
                        help="Sniffer TCP port (default: 9000)")

    # LED matrix hardware flags
    parser.add_argument("--led-rows",                 type=int,  default=64)
    parser.add_argument("--led-cols",                 type=int,  default=64)
    parser.add_argument("--led-chain",                type=int,  default=1,   dest="led_chain")
    parser.add_argument("--led-parallel",             type=int,  default=1,   dest="led_parallel")
    parser.add_argument("--led-pwm-bits",             type=int,  default=7,   dest="led_pwm_bits")
    parser.add_argument("--led-pwm-dither-bits",      type=int,  default=1,   dest="led_pwm_dither_bits")
    parser.add_argument("--led-pwm-lsb-nanoseconds",  type=int,  default=50,  dest="led_pwm_lsb_nanoseconds")
    parser.add_argument("--led-slowdown-gpio",        type=int,  default=3,   dest="led_slowdown_gpio")
    parser.add_argument("--led-brightness",           type=int,  default=100, dest="led_brightness")
    parser.add_argument("--led-hardware-mapping",     default="regular",      dest="led_hardware_mapping")
    parser.add_argument("--led-show-refresh",         action="store_true",    dest="led_show_refresh")

    return parser.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────
def main():
    args = parse_args()

    options = RGBMatrixOptions()
    options.rows                = args.led_rows
    options.cols                = args.led_cols
    options.chain_length        = args.led_chain
    options.parallel            = args.led_parallel
    options.pwm_bits            = args.led_pwm_bits
    options.pwm_dither_bits     = args.led_pwm_dither_bits
    options.pwm_lsb_nanoseconds = args.led_pwm_lsb_nanoseconds
    options.gpio_slowdown       = args.led_slowdown_gpio
    options.brightness          = args.led_brightness
    options.hardware_mapping    = args.led_hardware_mapping
    options.show_refresh_rate   = args.led_show_refresh
    options.drop_privileges     = False

    matrix = RGBMatrix(options=options)
    cols   = matrix.width   # cols × chain_length
    rows   = matrix.height  # rows × parallel
    print(f"[*] Matrix: {cols}×{rows} pixels")
    print(f"[*] Connecting to sniffer at {args.ip}:{args.port}")

    manager  = PacketManager(cols=cols, rows=rows)
    incoming: queue.Queue = queue.Queue()

    threading.Thread(
        target=tcp_client,
        args=(args.ip, args.port, incoming),
        daemon=True,
        name="tcp-client",
    ).start()

    canvas     = matrix.CreateFrameCanvas()
    frame_sec  = 1.0 / TARGET_FPS

    try:
        while True:
            loop_start = time.monotonic()
            now        = loop_start * 1000  # ms

            # Drain incoming packets queued by the TCP thread
            while not incoming.empty():
                try:
                    manager.add(incoming.get_nowait())
                except queue.Empty:
                    break

            # Fire due flash bursts (iterate backwards so pop doesn't shift indices)
            i = len(flash_queue) - 1
            while i >= 0:
                entry = flash_queue[i]
                if now >= entry["at"]:
                    flash_queue.pop(i)
                    lit   = entry["get_all_lit"]()
                    count = min(entry["flash_grids"], len(lit))
                    for j in range(count):
                        k = j + (random.randint(0, len(lit) - j - 1) if len(lit) - j > 1 else 0)
                        lit[j], lit[k] = lit[k], lit[j]
                        lit[j].flash()
                i -= 1

            # Tick each packet; collect finished ones
            done = [p for p in manager.packets if p.tick(now)]
            for p in done:
                manager.on_gone(p)

            # Flush dirty pixels to the front canvas, capture which coords changed
            flushed = fb.flush(canvas)

            # Swap — canvas is now displayed; returned canvas is the back buffer
            canvas = matrix.SwapOnVSync(canvas)

            # Keep the back buffer in sync: apply the same pixels that just changed
            for (col, row) in flushed:
                r, g, b = fb._pixels.get((col, row), COLOR_BG)
                canvas.SetPixel(col, row, r, g, b)

            # Sleep for remainder of frame budget
            elapsed = time.monotonic() - loop_start
            sleep   = frame_sec - elapsed
            if sleep > 0:
                time.sleep(sleep)

    except KeyboardInterrupt:
        matrix.Clear()
        print("\n[*] Shutting down.")


if __name__ == "__main__":
    main()
