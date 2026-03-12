#!/usr/bin/env python3
"""
RGB Matrix renderer — rect-based art matching frontend/public/art/app.js

Listens for an incoming tunnel connection and renders neon rect bursts
onto a physical RGB LED matrix. All rect dimensions are expressed as
fractions of the display size so the art scales correctly to any matrix.

Usage:
    sudo python3 matrix.py --port 9001 --mode CASCADE \
        --led-chain=3 --led-parallel=3 --led-rows=64 --led-cols=64 \
        --led-pwm-bits=7 --led-pwm-dither-bits=1 \
        --led-slowdown-gpio=3 --led-pwm-lsb-nanoseconds=50 \
        --led-show-refresh
"""

import argparse
import json
import queue
import socket
import threading
import time

import numpy as np
from PIL import Image
from rgbmatrix import RGBMatrix, RGBMatrixOptions

TARGET_FPS = 30


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
                                        incoming.put_nowait((
                                            data.get("length", 0),
                                            data.get("direction"),
                                        ))
                                    except queue.Full:
                                        pass  # drop — main loop is behind
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
    parser.add_argument("--mode", choices=["NORMAL", "CASCADE"], default="NORMAL",
                        help="Animation mode: NORMAL (stream-Y) or CASCADE (sweep-line) (default: NORMAL)")

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

    if args.mode == "CASCADE":
        import mode_cascade as mode
    else:
        import mode_normal as mode

    print(f"[*] Mode: {args.mode}")

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

    canvas    = matrix.CreateFrameCanvas()
    frame_sec = 1.0 / TARGET_FPS

    try:
        while True:
            loop_start = time.monotonic()
            now        = loop_start * 1000

            # Drain incoming packets — cap at 10 per frame to avoid stalls
            for _ in range(10):
                try:
                    bytes_, direction = incoming.get_nowait()
                    mode.spawn_burst(bytes_, direction, matrix_w, matrix_h, now)
                except queue.Empty:
                    break

            # Render composite frame
            frame = mode.render_frame(matrix_w, matrix_h, now)

            # Write full frame in one C call — far faster than per-pixel SetPixel
            pil_img = Image.fromarray(frame, "RGB")
            canvas.SetImage(pil_img)

            canvas = matrix.SwapOnVSync(canvas)

            # Sync back-buffer with the same image (both hardware buffers must match)
            canvas.SetImage(pil_img)

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
