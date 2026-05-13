#!/usr/bin/env python3
"""Send spoofed NetFlow v5 packets to a Kentik universal-agent collector.

Each packet's source IP is spoofed to a different address from a configurable
range, which causes the receiving collector to register each spoofed source as
a new sampling device. That in turn gives `kentik-device-onboarder` something
to discover and onboard.

Requires root (raw sockets) and works only on Linux. Uses scapy if available;
otherwise falls back to a pure-stdlib raw-socket implementation.

Examples
--------
Send 50 packets, one each from 10.99.0.1 .. 10.99.0.50, to the local agent:

    sudo python3 send_spoofed_netflow.py \\
        --target 127.0.0.1 \\
        --src-cidr 10.99.0.0/24 \\
        --count 50

Continuously trickle one packet per second from a /28, forever:

    sudo python3 send_spoofed_netflow.py \\
        --target 10.0.0.5 \\
        --src-cidr 10.99.0.0/28 \\
        --count 0 \\
        --interval 1
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import random
import socket
import struct
import sys
import time

NETFLOW_V5_HEADER = struct.Struct("!HHIIIIBBH")
NETFLOW_V5_RECORD = struct.Struct("!IIIHHIIIIHHBBBBHHBBH")
NETFLOW_PORT_DEFAULT = 9995

# ── NetFlow v5 packet construction ─────────────────────────────────────────


def build_netflow_v5_packet(
    src_ips: list[int],
    dst_ip: int,
    flow_sequence: int,
    sysuptime_ms: int,
    *,
    records_per_packet: int = 10,
) -> bytes:
    """Build a NetFlow v5 packet with `records_per_packet` synthetic flows.

    `src_ips` and `dst_ip` are integers (host byte order); they populate the
    srcaddr/dstaddr fields of the flow records (NOT the IP header).
    """
    now = time.time()
    unix_secs = int(now)
    unix_nsecs = int((now - unix_secs) * 1_000_000_000)

    header = NETFLOW_V5_HEADER.pack(
        5,                  # version
        records_per_packet, # count
        sysuptime_ms,       # sysuptime (ms since boot of the exporter)
        unix_secs,          # unix_secs
        unix_nsecs,         # unix_nsecs
        flow_sequence,      # flow_sequence
        0,                  # engine_type
        0,                  # engine_id
        0,                  # sampling_interval
    )

    records = bytearray()
    for i in range(records_per_packet):
        src = src_ips[i % len(src_ips)]
        records += NETFLOW_V5_RECORD.pack(
            src,                            # srcaddr
            dst_ip,                         # dstaddr
            0,                              # nexthop
            1,                              # input snmp ifindex
            2,                              # output snmp ifindex
            random.randint(1, 1000),        # dPkts
            random.randint(64, 1_500_000),  # dOctets
            sysuptime_ms - 60_000,          # first (ms)
            sysuptime_ms,                   # last (ms)
            random.randint(1024, 65535),    # srcport
            random.choice((80, 443, 22, 53, 8080)),  # dstport
            0,                              # pad1
            0x18,                           # tcp_flags (ACK|PSH)
            6,                              # prot (TCP)
            0,                              # tos
            65001,                          # src_as
            65002,                          # dst_as
            24,                             # src_mask
            24,                             # dst_mask
            0,                              # pad2
        )
    return bytes(header) + bytes(records)


# ── Raw IP/UDP packet with spoofed source ──────────────────────────────────


def _ip_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def _udp_checksum(src_ip: int, dst_ip: int, udp_header: bytes, payload: bytes) -> int:
    pseudo = struct.pack(
        "!IIBBH",
        src_ip,
        dst_ip,
        0,
        socket.IPPROTO_UDP,
        len(udp_header) + len(payload),
    )
    return _ip_checksum(pseudo + udp_header + payload)


def build_raw_udp_packet(
    src_ip: int, src_port: int, dst_ip: int, dst_port: int, payload: bytes,
) -> bytes:
    udp_len = 8 + len(payload)
    # UDP with checksum=0 first, compute, then patch.
    udp_header = struct.pack("!HHHH", src_port, dst_port, udp_len, 0)
    udp_csum = _udp_checksum(src_ip, dst_ip, udp_header, payload)
    if udp_csum == 0:
        udp_csum = 0xFFFF
    udp_header = struct.pack("!HHHH", src_port, dst_port, udp_len, udp_csum)

    total_len = 20 + udp_len
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        (4 << 4) | 5,                          # version+IHL
        0,                                     # tos
        total_len,
        random.randint(0, 0xFFFF),             # id
        0,                                     # frag
        64,                                    # ttl
        socket.IPPROTO_UDP,
        0,                                     # checksum (kernel fills if 0)
        src_ip.to_bytes(4, "big"),
        dst_ip.to_bytes(4, "big"),
    )
    # Most kernels recompute the IP checksum when checksum field is 0, but we
    # set it explicitly for portability.
    ip_csum = _ip_checksum(ip_header)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        (4 << 4) | 5,
        0,
        total_len,
        struct.unpack_from("!H", ip_header, 4)[0],
        0,
        64,
        socket.IPPROTO_UDP,
        ip_csum,
        src_ip.to_bytes(4, "big"),
        dst_ip.to_bytes(4, "big"),
    )

    return ip_header + udp_header + payload


def open_raw_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
    return s


# ── Main ───────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Send spoofed NetFlow v5 packets to trigger device onboarding.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--target", default="127.0.0.1",
                   help="Destination IP (the universal agent / collector).")
    p.add_argument("--port", type=int, default=NETFLOW_PORT_DEFAULT,
                   help="Destination UDP port.")
    p.add_argument("--src-cidr", default="10.99.0.0/24",
                   help="CIDR of source IPs to spoof. The first usable address "
                        "and onward are used as packet source IPs.")
    p.add_argument("--src-port", type=int, default=2055,
                   help="Spoofed UDP source port.")
    p.add_argument("--count", type=int, default=50,
                   help="Number of packets to send. 0 = forever.")
    p.add_argument("--interval", type=float, default=0.05,
                   help="Seconds to sleep between packets.")
    p.add_argument("--records-per-packet", type=int, default=10,
                   help="NetFlow v5 records per packet (1-30).")
    p.add_argument("--flow-dst", default="8.8.8.8",
                   help="dstaddr field placed inside each flow record.")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed for reproducible payloads.")
    p.add_argument("--dry-run", action="store_true",
                   help="Build packets and print sizes but don't send.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    if not 1 <= args.records_per_packet <= 30:
        print("error: --records-per-packet must be 1..30", file=sys.stderr)
        return 2

    network = ipaddress.ip_network(args.src_cidr, strict=False)
    if network.version != 4:
        print("error: --src-cidr must be IPv4", file=sys.stderr)
        return 2
    spoof_ips = [int(h) for h in network.hosts()]
    if not spoof_ips:
        spoof_ips = [int(network.network_address)]
    print(f"spoofing from {len(spoof_ips)} source IP(s) "
          f"({network.network_address} .. {network.broadcast_address})")

    dst_ip = int(ipaddress.IPv4Address(args.target))
    flow_dst = int(ipaddress.IPv4Address(args.flow_dst))

    if not args.dry_run and os.geteuid() != 0:
        print("error: raw sockets require root. Re-run with sudo.", file=sys.stderr)
        return 2

    sock = None if args.dry_run else open_raw_socket()
    boot_ms = int(time.time() * 1000) - 60_000  # pretend exporter was up 60s
    sequence = 0
    sent = 0
    try:
        while args.count == 0 or sent < args.count:
            spoof_ip = spoof_ips[sent % len(spoof_ips)]
            sysuptime_ms = int(time.time() * 1000) - boot_ms
            payload = build_netflow_v5_packet(
                src_ips=[int(ipaddress.IPv4Address("10.42.0.1")) + i
                         for i in range(args.records_per_packet)],
                dst_ip=flow_dst,
                flow_sequence=sequence,
                sysuptime_ms=sysuptime_ms,
                records_per_packet=args.records_per_packet,
            )
            sequence += args.records_per_packet

            packet = build_raw_udp_packet(
                src_ip=spoof_ip,
                src_port=args.src_port,
                dst_ip=dst_ip,
                dst_port=args.port,
                payload=payload,
            )

            if args.dry_run:
                if sent < 3:
                    print(f"  [{sent}] would send {len(packet)} bytes "
                          f"src={ipaddress.IPv4Address(spoof_ip)} -> {args.target}:{args.port}")
            else:
                sock.sendto(packet, (args.target, 0))
                if sent % 10 == 0:
                    print(f"  sent {sent + 1} packet(s); last src="
                          f"{ipaddress.IPv4Address(spoof_ip)}")

            sent += 1
            if args.interval > 0:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if sock is not None:
            sock.close()

    print(f"done; sent {sent} packet(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
