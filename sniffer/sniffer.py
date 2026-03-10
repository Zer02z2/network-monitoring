#!/usr/bin/env python3
"""
Network Traffic Sniffer

Discovers destination IPs two ways:
  1. TLS SNI  — filters tls.handshake.extensions_server_name for each name
  2. DNS      — filters dns.qry.name for each name, extracts A/AAAA answers

Once an IP is added to known_ips, a single always-on traffic capture thread
immediately starts reporting all TCP/UDP packets to/from that IP.

Usage:
    python sniffer.py -names openai.com chatgpt.com [-port 9000] [-interface en0]

Clients connect to the TCP port and receive a stream of JSON objects, one per line.
"""

import argparse
import asyncio
import json
import threading
from datetime import datetime, timezone

import pyshark


def parse_args():
    parser = argparse.ArgumentParser(description="Network Traffic Sniffer")
    parser.add_argument(
        "-names", nargs="+", required=True,
        help="Domain names to watch via TLS SNI and DNS (e.g. openai.com chatgpt.com)"
    )
    parser.add_argument(
        "-port", type=int, default=9000,
        help="TCP port to stream JSON traffic on (default: 9000)"
    )
    parser.add_argument(
        "-interface", default="en0",
        help="Network interface to capture on (default: en0)"
    )
    return parser.parse_args()


class Sniffer:
    def __init__(self, names: list[str], broadcast_port: int, interface: str):
        self.names = names
        self.broadcast_port = broadcast_port
        self.interface = interface

        # Shared set — written by discovery thread, read by traffic thread.
        # Python set reads/writes of references are GIL-safe for this use case.
        self.known_ips: set[str] = set()

        self._writers: list[asyncio.StreamWriter] = []
        self._writers_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self):
        self._loop = asyncio.get_running_loop()

        server = await asyncio.start_server(
            self._handle_client, "0.0.0.0", self.broadcast_port
        )
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        print(f"[*] TCP broadcast server listening on {addrs}")

        # Two long-lived daemon threads — they never need to restart.
        threading.Thread(target=self._discovery_capture_thread, daemon=True).start()
        threading.Thread(target=self._traffic_capture_thread, daemon=True).start()

        async with server:
            await server.serve_forever()

    # ------------------------------------------------------------------
    # TCP broadcast server
    # ------------------------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        print(f"[+] Client connected: {addr}")
        async with self._writers_lock:
            self._writers.append(writer)

        # Catch up: tell the new client about IPs we already know
        if self.known_ips:
            payload = json.dumps({
                "type": "existing_ips",
                "ips": list(self.known_ips),
                "timestamp": _now(),
            }) + "\n"
            try:
                writer.write(payload.encode())
                await writer.drain()
            except Exception:
                pass

        try:
            await reader.read(1)   # blocks until the client disconnects
        except Exception:
            pass
        finally:
            async with self._writers_lock:
                try:
                    self._writers.remove(writer)
                except ValueError:
                    pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            print(f"[-] Client disconnected: {addr}")

    def _broadcast(self, data: dict):
        """Thread-safe: schedule a broadcast onto the asyncio event loop."""
        if self._loop is None or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._async_broadcast(data), self._loop)

    async def _async_broadcast(self, data: dict):
        message = (json.dumps(data) + "\n").encode()
        dead: list[asyncio.StreamWriter] = []
        async with self._writers_lock:
            for writer in list(self._writers):
                try:
                    writer.write(message)
                    await writer.drain()
                except Exception:
                    dead.append(writer)
            for w in dead:
                try:
                    self._writers.remove(w)
                except ValueError:
                    pass

    # ------------------------------------------------------------------
    # Packet helper
    # ------------------------------------------------------------------

    @staticmethod
    def _packet_info(packet, event_type: str = "traffic") -> dict:
        data: dict = {
            "type": event_type,
            "timestamp": _now(),
            "protocol": packet.highest_layer,
            "length": int(packet.length) if hasattr(packet, "length") else 0,
        }

        if hasattr(packet, "ip"):
            data["src_ip"] = packet.ip.src
            data["dst_ip"] = packet.ip.dst
        elif hasattr(packet, "ipv6"):
            data["src_ip"] = packet.ipv6.src
            data["dst_ip"] = packet.ipv6.dst

        if hasattr(packet, "tcp"):
            data["transport"] = "TCP"
            data["src_port"] = int(packet.tcp.srcport)
            data["dst_port"] = int(packet.tcp.dstport)
        elif hasattr(packet, "udp"):
            data["transport"] = "UDP"
            data["src_port"] = int(packet.udp.srcport)
            data["dst_port"] = int(packet.udp.dstport)

        return data

    # ------------------------------------------------------------------
    # Thread 1 — Discovery (SNI + DNS)
    # Runs forever; updates known_ips; broadcasts new_ip events.
    # ------------------------------------------------------------------

    def _build_discovery_filter(self) -> str:
        sni_parts = [
            f'tls.handshake.extensions_server_name contains "{name}"'
            for name in self.names
        ]
        dns_parts = [
            f'dns.qry.name contains "{name}"'
            for name in self.names
        ]
        return " or ".join(sni_parts + dns_parts)

    def _discovery_capture_thread(self):
        disc_filter = self._build_discovery_filter()
        print(f"[*] Discovery filter : {disc_filter}")
        print(f"[*] Interface        : {self.interface}")

        try:
            capture = pyshark.LiveCapture(
                interface=self.interface,
                display_filter=disc_filter,
            )
            for packet in capture.sniff_continuously():
                if hasattr(packet, "dns"):
                    self._handle_dns_packet(packet)
                else:
                    self._handle_sni_packet(packet)
        except Exception as exc:
            print(f"[!] Discovery capture error: {exc}")

    def _handle_sni_packet(self, packet):
        ip_layer = (
            packet.ip    if hasattr(packet, "ip")    else
            packet.ipv6  if hasattr(packet, "ipv6")  else None
        )
        if ip_layer is None:
            return

        dst_ip = ip_layer.dst
        if dst_ip in self.known_ips:
            return

        self.known_ips.add(dst_ip)
        print(f"[+] New IP via SNI  : {dst_ip}")

        info = self._packet_info(packet, event_type="new_ip")
        info["discovered_ip"] = dst_ip
        info["discovery_source"] = "sni"
        try:
            info["sni"] = packet.tls.handshake_extensions_server_name
        except Exception:
            pass
        self._broadcast(info)

    def _handle_dns_packet(self, packet):
        dns = packet.dns
        try:
            if dns.flags_response != "1":
                return
        except AttributeError:
            return

        query_name = ""
        try:
            query_name = dns.qry_name
        except AttributeError:
            pass

        for ip in self._extract_dns_ips(dns):
            if ip in self.known_ips:
                continue
            self.known_ips.add(ip)
            print(f"[+] New IP via DNS  : {ip}  (query: {query_name})")

            info = self._packet_info(packet, event_type="new_ip")
            info["discovered_ip"] = ip
            info["discovery_source"] = "dns"
            info["dns_query"] = query_name
            self._broadcast(info)

    @staticmethod
    def _extract_dns_ips(dns_layer) -> list[str]:
        ips: list[str] = []
        # Preferred: _all_fields gives every repeated A/AAAA value
        try:
            for key, val in dns_layer._all_fields.items():
                if key in ("dns.a", "dns.aaaa"):
                    for entry in (val if isinstance(val, list) else [val]):
                        ip = str(entry).strip()
                        if ip:
                            ips.append(ip)
            if ips:
                return ips
        except Exception:
            pass
        # Fallback: attribute access
        for attr in ("a", "aaaa"):
            try:
                val = getattr(dns_layer, attr, None)
                if val:
                    ips.append(str(val).strip())
            except Exception:
                pass
        return ips

    # ------------------------------------------------------------------
    # Thread 2 — Traffic monitor
    # Single always-on capture; no restarts ever.
    # Captures all TCP+UDP, then checks Python-side against known_ips.
    # Handles both IPv4 and IPv6 transparently.
    # ------------------------------------------------------------------

    def _traffic_capture_thread(self):
        print(f"[*] Traffic capture  : bpf=tcp or udp  interface={self.interface}")
        try:
            capture = pyshark.LiveCapture(
                interface=self.interface,
                bpf_filter="tcp or udp",
            )
            for packet in capture.sniff_continuously():
                # Resolve IP layer (handles both IPv4 and IPv6)
                src_ip = dst_ip = None
                if hasattr(packet, "ip"):
                    src_ip = packet.ip.src
                    dst_ip = packet.ip.dst
                elif hasattr(packet, "ipv6"):
                    src_ip = packet.ipv6.src
                    dst_ip = packet.ipv6.dst

                if src_ip in self.known_ips or dst_ip in self.known_ips:
                    self._broadcast(self._packet_info(packet, event_type="traffic"))

        except Exception as exc:
            print(f"[!] Traffic capture error: {exc}")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main():
    args = parse_args()
    print(f"[*] Watching (SNI + DNS): {', '.join(args.names)}")
    sniffer = Sniffer(
        names=args.names,
        broadcast_port=args.port,
        interface=args.interface,
    )
    try:
        asyncio.run(sniffer.start())
    except KeyboardInterrupt:
        print("\n[*] Shutting down.")


if __name__ == "__main__":
    main()
