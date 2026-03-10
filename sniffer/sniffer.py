#!/usr/bin/env python3
"""
Network Traffic Sniffer

For each -names flag, one dedicated tshark discovery process runs:
    tls.handshake.extensions_server_name contains "<name>"
    OR dns.qry.name contains "<name>"

Because each thread owns exactly one flag name, matched_names is always
determined by which thread fired — no text-matching heuristics needed.

One separate always-on traffic thread (bpf: tcp or udp) watches all packets
and reports any whose src/dst IP is in known_ips.

Usage:
    python sniffer.py -names openai.com chatgpt.com [-port 9000] [-interface en0]
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

        # Shared structures — written by discovery threads, read by traffic thread.
        # Plain dict/set mutations are GIL-safe for this use case.
        self.known_ips: set[str] = set()
        # ip → list of flag names whose filter matched a packet to/from that ip
        self.ip_to_names: dict[str, list[str]] = {}

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
        print(f"[*] Interface : {self.interface}")

        # One discovery thread per flag name + one shared traffic thread
        for name in self.names:
            threading.Thread(
                target=self._discovery_thread,
                args=(name,),
                daemon=True,
                name=f"discovery-{name}",
            ).start()

        threading.Thread(
            target=self._traffic_capture_thread,
            daemon=True,
            name="traffic",
        ).start()

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

        # Send currently-known IPs to the newly connected client
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
            await reader.read(1)  # blocks until the client disconnects
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
    # Per-flag discovery thread
    # Each flag name gets its own tshark process with a dedicated filter.
    # The flag_name is known at construction time — no text matching needed.
    # ------------------------------------------------------------------

    def _discovery_thread(self, flag_name: str):
        disc_filter = (
            f'tls.handshake.extensions_server_name contains "{flag_name}"'
            f' or dns.qry.name contains "{flag_name}"'
        )
        print(f"[*] [{flag_name}] discovery filter: {disc_filter}")

        try:
            capture = pyshark.LiveCapture(
                interface=self.interface,
                display_filter=disc_filter,
            )
            for packet in capture.sniff_continuously():
                if hasattr(packet, "dns"):
                    self._handle_dns_packet(packet, flag_name)
                else:
                    self._handle_sni_packet(packet, flag_name)
        except Exception as exc:
            print(f"[!] [{flag_name}] discovery error: {exc}")

    def _register_ip(self, ip: str, flag_name: str):
        """Add ip to known_ips and record which flag triggered it.

        An IP can legitimately be registered by multiple flags if the same
        CDN endpoint serves several of the monitored domains. Merge rather
        than overwrite so both attributions are visible.
        """
        self.known_ips.add(ip)
        existing = self.ip_to_names.get(ip, [])
        if flag_name not in existing:
            self.ip_to_names[ip] = existing + [flag_name]

    def _handle_sni_packet(self, packet, flag_name: str):
        ip_layer = (
            packet.ip   if hasattr(packet, "ip")   else
            packet.ipv6 if hasattr(packet, "ipv6") else None
        )
        if ip_layer is None:
            return

        dst_ip = ip_layer.dst
        is_new = dst_ip not in self.known_ips
        self._register_ip(dst_ip, flag_name)

        if not is_new:
            return  # already reported; skip duplicate broadcast

        print(f"[+] [{flag_name}] new IP via SNI : {dst_ip}")

        sni = ""
        try:
            sni = packet.tls.handshake_extensions_server_name
        except Exception:
            pass

        info = self._packet_info(packet, event_type="new_ip")
        info["discovered_ip"]   = dst_ip
        info["discovery_source"] = "sni"
        info["matched_names"]   = self.ip_to_names[dst_ip]
        if sni:
            info["sni"] = sni
        self._broadcast(info)

    def _handle_dns_packet(self, packet, flag_name: str):
        dns = packet.dns

        # Only process responses
        try:
            if dns.flags_response != "1":
                return
        except AttributeError:
            return

        # Read dns.qry.name via _all_fields to avoid the pyshark attribute-name
        # mismatch (packet.dns.qry_name looks up "dns.qry_name" but the tshark
        # field abbreviation is "dns.qry.name").
        query_name = ""
        try:
            val = dns._all_fields.get("dns.qry.name", "")
            query_name = str(val[0] if isinstance(val, list) else val).strip()
        except Exception:
            pass

        for ip in self._extract_dns_ips(dns):
            is_new = ip not in self.known_ips
            self._register_ip(ip, flag_name)

            if not is_new:
                continue  # already reported

            print(f"[+] [{flag_name}] new IP via DNS : {ip}  (query: {query_name})")

            info = self._packet_info(packet, event_type="new_ip")
            info["discovered_ip"]    = ip
            info["discovery_source"] = "dns"
            info["dns_query"]        = query_name
            info["matched_names"]    = self.ip_to_names[ip]
            self._broadcast(info)

    @staticmethod
    def _extract_dns_ips(dns_layer) -> list[str]:
        """Return all A and AAAA record values from a DNS layer."""
        ips: list[str] = []
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
    # Traffic monitor — single always-on capture
    # Uses a broad BPF filter; checks Python-side against known_ips.
    # No restarts needed: as known_ips grows the check automatically covers
    # new IPs without touching the capture.
    # ------------------------------------------------------------------

    def _traffic_capture_thread(self):
        print(f"[*] Traffic capture  : bpf=tcp or udp")
        try:
            capture = pyshark.LiveCapture(
                interface=self.interface,
                bpf_filter="tcp or udp",
            )
            for packet in capture.sniff_continuously():
                src_ip = dst_ip = None
                if hasattr(packet, "ip"):
                    src_ip = packet.ip.src
                    dst_ip = packet.ip.dst
                elif hasattr(packet, "ipv6"):
                    src_ip = packet.ipv6.src
                    dst_ip = packet.ipv6.dst

                # Prefer dst for attribution; fall back to src (return traffic)
                matched_ip = (
                    dst_ip if dst_ip in self.known_ips else
                    src_ip if src_ip in self.known_ips else
                    None
                )
                if matched_ip is not None:
                    info = self._packet_info(packet, event_type="traffic")
                    info["matched_names"] = self.ip_to_names.get(matched_ip, [])
                    self._broadcast(info)

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
