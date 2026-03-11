#!/usr/bin/env python3
"""
Tunnel: connects to the local sniffer TCP stream and forwards every message
to a remote target (e.g. a Raspberry Pi running matrix.py).

Usage:
    python3 tunnel.py --target-host raspberrypi.local --target-port 9001
    python3 tunnel.py --target-host 192.168.1.42 --target-port 9001 --sniffer-port 9000
"""

import argparse
import socket
import time


def parse_args():
    parser = argparse.ArgumentParser(description="Sniffer → Matrix tunnel")
    parser.add_argument("--target-host", required=True,
                        help="Hostname or IP of the target (e.g. raspberrypi.local)")
    parser.add_argument("--target-port", type=int, required=True,
                        help="TCP port on the target to forward to")
    parser.add_argument("--sniffer-port", type=int, default=9000,
                        help="Local port the sniffer is broadcasting on (default: 9000)")
    return parser.parse_args()


def run(sniffer_port: int, target_host: str, target_port: int):
    while True:
        # ── Connect to sniffer ────────────────────────────────────────────
        try:
            sniffer_sock = socket.create_connection(("127.0.0.1", sniffer_port))
            print(f"[*] Connected to sniffer on localhost:{sniffer_port}")
        except Exception as e:
            print(f"[!] Cannot connect to sniffer: {e}. Retrying in 3 s...")
            time.sleep(3)
            continue

        # ── Connect to target ─────────────────────────────────────────────
        try:
            target_sock = socket.create_connection((target_host, target_port))
            print(f"[*] Connected to target {target_host}:{target_port}")
        except Exception as e:
            print(f"[!] Cannot connect to target: {e}. Retrying in 3 s...")
            sniffer_sock.close()
            time.sleep(3)
            continue

        # ── Forward bytes sniffer → target ────────────────────────────────
        try:
            while True:
                data = sniffer_sock.recv(4096)
                if not data:
                    break
                target_sock.sendall(data)
        except Exception as e:
            print(f"[!] Forwarding error: {e}")
        finally:
            sniffer_sock.close()
            target_sock.close()

        print("[!] Connection dropped. Reconnecting...")
        time.sleep(1)


def main():
    args = parse_args()
    print(f"[*] Tunnel: localhost:{args.sniffer_port} → {args.target_host}:{args.target_port}")
    run(args.sniffer_port, args.target_host, args.target_port)


if __name__ == "__main__":
    main()
