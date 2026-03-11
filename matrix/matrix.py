#!/usr/bin/env python3
"""
RGB Matrix renderer — rect-based art matching frontend/public/art/app.js

Listens for an incoming tunnel connection and renders neon rect bursts
onto a physical RGB LED matrix. All rect dimensions are expressed as
fractions of the display size so the art scales correctly to any matrix.

Usage:
    sudo python3 matrix.py --port 9001 \
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

import numpy as np
from rgbmatrix import RGBMatrix, RGBMatrixOptions

# ── TUNABLE CONFIG ─────────────────────────────────────────────────────────
RECT_BASE_COUNT   = 2
RECT_SCALE        = 0.012
RECT_MAX_COUNT    = 28

# Rect dimensions as fractions of the display — matches app.js on a 1920×1080 canvas
RECT_MIN_W_FRAC   = 20  / 1920   # ~0.0104
RECT_MAX_W_FRAC   = 700 / 1920   # ~0.3646
RECT_MIN_H_FRAC   = 2   / 1080   # ~0.00185
RECT_MAX_H_FRAC   = 200 / 1080   # ~0.185

RECT_LIFETIME     = 650    # ms — base lifetime
RECT_LIFETIME_VAR = 0.7    # randomness factor on lifetime
RECT_ALPHA_MIN    = 0.22
RECT_ALPHA_MAX    = 0.75
RED_CHANCE        = 0.78   # probability of neon red vs neon blue
STROKE_CHANCE     = 0.22   # probability of outline-only rect

Y_SPREAD          = 0.28   # spread around stream Y as fraction of display height
Y_RANDOM_CHANCE   = 0.12
Y_OPPOSITE_CHANCE = 0.09
STREAM_PER_BYTE   = 0.00006

FLASH_THRESHOLD   = 600    # bytes — triggers white flash rects
FLASH_ALPHA_MIN   = 0.55
FLASH_ALPHA_MAX   = 0.92
FLASH_LIFETIME    = 260    # ms
FLASH_MAX_COUNT   = 3

MAX_RECTS         = 350    # global cap — oldest pruned when exceeded
TARGET_FPS        = 30     # conservative for Pi CPU budget

NEON_RED   = (255,  15,  45)
NEON_BLUE  = ( 20, 130, 255)
NEON_WHITE = (255, 255, 255)
# ──────────────────────────────────────────────────────────────────────────


def now_ms() -> float:
    return time.monotonic() * 1000


# ── Global rect list and stream position ──────────────────────────────────
_stream_y: float = 0.05
_rects: list[dict] = []  # {x, y, w, h, rgb, alpha, created_at, die_at, stroke, is_flash}


def _pick_y(base_y: float, h: int, matrix_h: int) -> int:
    r = random.random()
    if r < Y_RANDOM_CHANCE:
        return int(random.random() * max(0, matrix_h - h))
    elif r < Y_RANDOM_CHANCE + Y_OPPOSITE_CHANCE:
        center = matrix_h - base_y
    else:
        center = base_y
    spread = Y_SPREAD * matrix_h
    y = center + (random.random() + random.random() - 1) * spread
    return int(max(0, min(matrix_h - h, y)))


def spawn_burst(bytes_: int, matrix_w: int, matrix_h: int):
    global _stream_y
    base_y = _stream_y * matrix_h
    count  = min(RECT_MAX_COUNT, RECT_BASE_COUNT + int(bytes_ * RECT_SCALE))
    now    = now_ms()

    rect_min_w = max(1, int(RECT_MIN_W_FRAC * matrix_w))
    rect_max_w = max(rect_min_w + 1, int(RECT_MAX_W_FRAC * matrix_w))
    rect_min_h = max(1, int(RECT_MIN_H_FRAC * matrix_h))
    rect_max_h = max(rect_min_h + 1, int(RECT_MAX_H_FRAC * matrix_h))

    for _ in range(count):
        scale_w = min(1.0, bytes_ / 2000)
        scale_h = min(1.0, bytes_ / 3000)
        max_w   = rect_min_w + scale_w * (rect_max_w - rect_min_w)
        max_h   = rect_min_h + scale_h * (rect_max_h - rect_min_h)
        w = max(1, int(rect_min_w + random.random() * (max_w - rect_min_w)))
        h = max(1, int(rect_min_h + random.random() * (max_h - rect_min_h)))
        x = int(random.random() * max(0, matrix_w - w))
        y = _pick_y(base_y, h, matrix_h)
        rgb   = NEON_RED if random.random() < RED_CHANCE else NEON_BLUE
        alpha = RECT_ALPHA_MIN + random.random() * (RECT_ALPHA_MAX - RECT_ALPHA_MIN)
        life  = RECT_LIFETIME * (1 - RECT_LIFETIME_VAR * 0.5 + random.random() * RECT_LIFETIME_VAR)
        _rects.append({
            'x': x, 'y': y, 'w': w, 'h': h,
            'rgb': rgb, 'alpha': alpha,
            'created_at': now, 'die_at': now + life,
            'stroke': random.random() < STROKE_CHANCE,
            'is_flash': False,
        })

    if bytes_ >= FLASH_THRESHOLD:
        intensity   = min(1.0, (bytes_ - FLASH_THRESHOLD) / 5000)
        flash_count = 1 + int(intensity * (FLASH_MAX_COUNT - 1))
        for _ in range(flash_count):
            fw = int(matrix_w * (0.25 + random.random() * 0.70))
            fh = int(matrix_h * (0.03 + intensity * 0.30 + random.random() * 0.12))
            fw = max(1, min(matrix_w, fw))
            fh = max(1, min(matrix_h, fh))
            fx = int(random.random() * max(0, matrix_w - fw))
            fy = _pick_y(base_y, fh, matrix_h)
            fa = FLASH_ALPHA_MIN + intensity * (FLASH_ALPHA_MAX - FLASH_ALPHA_MIN) * random.random()
            _rects.append({
                'x': fx, 'y': fy, 'w': fw, 'h': fh,
                'rgb': NEON_WHITE, 'alpha': fa,
                'created_at': now,
                'die_at': now + FLASH_LIFETIME * (0.6 + random.random() * 0.8),
                'stroke': random.random() < 0.45,
                'is_flash': True,
            })

    _stream_y = (_stream_y + bytes_ * STREAM_PER_BYTE) % 1.0

    if len(_rects) > MAX_RECTS:
        del _rects[:len(_rects) - MAX_RECTS]


def render_frame(matrix_w: int, matrix_h: int, now: float) -> np.ndarray:
    """
    Composite all live rects oldest→newest onto a black canvas.
    Alpha is applied as: pixel = pixel * (1-alpha) + color * alpha
    which is correct because the background is black — identical to the JS canvas.
    Returns a uint8 HxWx3 array.
    """
    frame = np.zeros((matrix_h, matrix_w, 3), dtype=np.float32)

    i = 0
    while i < len(_rects):
        r = _rects[i]
        if now >= r['die_at']:
            _rects.pop(i)
            continue

        age      = now - r['created_at']
        lifetime = r['die_at'] - r['created_at']

        if r['is_flash']:
            # Cosine ease — hits instantly, fades smoothly (matches JS)
            t     = (age / lifetime) * math.pi * 0.5
            alpha = r['alpha'] * math.cos(t) ** 2
        else:
            # Hold alpha for first 15%, then linear fade to 0 (matches JS)
            hold   = lifetime * 0.15
            fade_t = 0.0 if age < hold else (age - hold) / (lifetime - hold)
            alpha  = r['alpha'] * max(0.0, 1.0 - fade_t)

        x1 = max(0, r['x'])
        y1 = max(0, r['y'])
        x2 = min(matrix_w, r['x'] + r['w'])
        y2 = min(matrix_h, r['y'] + r['h'])
        if x1 >= x2 or y1 >= y2:
            i += 1
            continue

        color = np.array(r['rgb'], dtype=np.float32)

        if r['stroke']:
            # Draw only the border of the rect
            frame[y1,   x1:x2] = frame[y1,   x1:x2] * (1 - alpha) + color * alpha
            if y2 - 1 != y1:
                frame[y2-1, x1:x2] = frame[y2-1, x1:x2] * (1 - alpha) + color * alpha
            if y2 - y1 > 2:
                frame[y1+1:y2-1, x1]   = frame[y1+1:y2-1, x1]   * (1 - alpha) + color * alpha
                if x2 - 1 != x1:
                    frame[y1+1:y2-1, x2-1] = frame[y1+1:y2-1, x2-1] * (1 - alpha) + color * alpha
        else:
            frame[y1:y2, x1:x2] = frame[y1:y2, x1:x2] * (1 - alpha) + color * alpha

        i += 1

    return np.clip(frame, 0, 255).astype(np.uint8)


# ── TCP server — listens for tunnel connection in background thread ────────
def tcp_server(port: int, incoming: queue.Queue):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(1)
        print(f"[*] Listening for tunnel on port {port}")
        while True:
            try:
                conn, addr = srv.accept()
                print(f"[+] Tunnel connected from {addr}")
                with conn:
                    buf = ""
                    while True:
                        chunk = conn.recv(4096).decode("utf-8", errors="ignore")
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
                                    try:
                                        incoming.put_nowait(data.get("length", 0))
                                    except queue.Full:
                                        pass  # drop — main loop is behind, don't block
                            except json.JSONDecodeError:
                                pass
                print(f"[-] Tunnel disconnected from {addr}")
            except Exception as e:
                print(f"[!] Server error: {e}")
                time.sleep(1)


# ── Argument parsing ──────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="RGB Matrix Network Art")

    parser.add_argument("--port", type=int, default=9001,
                        help="TCP port to listen on for tunnel connection (default: 9001)")

    parser.add_argument("--led-rows",                type=int,  default=64)
    parser.add_argument("--led-cols",                type=int,  default=64)
    parser.add_argument("--led-chain",               type=int,  default=1,   dest="led_chain")
    parser.add_argument("--led-parallel",            type=int,  default=1,   dest="led_parallel")
    parser.add_argument("--led-pwm-bits",            type=int,  default=7,   dest="led_pwm_bits")
    parser.add_argument("--led-pwm-dither-bits",     type=int,  default=1,   dest="led_pwm_dither_bits")
    parser.add_argument("--led-pwm-lsb-nanoseconds", type=int,  default=50,  dest="led_pwm_lsb_nanoseconds")
    parser.add_argument("--led-slowdown-gpio",       type=int,  default=3,   dest="led_slowdown_gpio")
    parser.add_argument("--led-brightness",          type=int,  default=100, dest="led_brightness")
    parser.add_argument("--led-hardware-mapping",    default="regular",      dest="led_hardware_mapping")
    parser.add_argument("--led-show-refresh",        action="store_true",    dest="led_show_refresh")

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

    matrix   = RGBMatrix(options=options)
    matrix_w = matrix.width
    matrix_h = matrix.height
    print(f"[*] Matrix: {matrix_w}×{matrix_h} pixels")

    incoming: queue.Queue = queue.Queue(maxsize=50)
    threading.Thread(
        target=tcp_server,
        args=(args.port, incoming),
        daemon=True,
        name="tcp-server",
    ).start()

    canvas     = matrix.CreateFrameCanvas()
    frame_sec  = 1.0 / TARGET_FPS
    prev_frame = np.zeros((matrix_h, matrix_w, 3), dtype=np.uint8)

    try:
        while True:
            loop_start = time.monotonic()
            now        = loop_start * 1000

            # Drain incoming packets — cap at 10 per frame to avoid stalls
            for _ in range(10):
                try:
                    spawn_burst(incoming.get_nowait(), matrix_w, matrix_h)
                except queue.Empty:
                    break

            # Render composite frame
            frame = render_frame(matrix_w, matrix_h, now)

            # Write only pixels that changed since last frame to the front canvas
            diff_mask = np.any(frame != prev_frame, axis=2)
            ys, xs    = np.where(diff_mask)
            for yi, xi in zip(ys.tolist(), xs.tolist()):
                canvas.SetPixel(int(xi), int(yi),
                                int(frame[yi, xi, 0]),
                                int(frame[yi, xi, 1]),
                                int(frame[yi, xi, 2]))

            prev_frame = frame
            canvas     = matrix.SwapOnVSync(canvas)

            # Sync back-buffer: apply the same changed pixels so both buffers match
            for yi, xi in zip(ys.tolist(), xs.tolist()):
                canvas.SetPixel(int(xi), int(yi),
                                int(frame[yi, xi, 0]),
                                int(frame[yi, xi, 1]),
                                int(frame[yi, xi, 2]))

            # SwapOnVSync blocks for hardware vsync; sleep any remaining budget
            elapsed   = time.monotonic() - loop_start
            remaining = frame_sec - elapsed
            if remaining > 0:
                time.sleep(remaining)

    except KeyboardInterrupt:
        matrix.Clear()
        print("\n[*] Shutting down.")


if __name__ == "__main__":
    main()
