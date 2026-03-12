"""
satellite/leo_relay.py — Process 3: LEO Satellite Relay.

Transport: UDP, Port 5003
Acts as a transparent relay between turbine (Ports 5001/5002) and
ground station (Port 5004), applying the full LEO channel model.

Features:
  - Applies latency, jitter, packet loss, and bandwidth throttling
  - Tracks orbital visibility windows and queues frames during blackout
  - Logs channel statistics for monitoring
  - Relays both uplink (turbine → ground) and downlink (ground → turbine)
"""

import json
import queue
import select
import socket
import struct
import threading
import time
import logging
import argparse

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    LEO_RELAY_PORT,
    TURBINE_SENSOR_PORT,
    GROUND_CONTROL_PORT,
    LOCALHOST,
)
from protocol.frame import build_frame, parse_frame
from protocol.message_types import MSG_STATUS, MSG_HEARTBEAT
from protocol.channel_model import LEOChannel
from utils.logger import setup_logging

logger = setup_logging("leo_relay")

# Blackout queue hold time — discard queued messages older than this
BLACKOUT_MAX_AGE_S = 120


class LEORelay:
    """
    UDP relay that passes frames through the LEO channel model.

    Uplink:   turbine    → relay (port 5003) → channel → ground
    Downlink: ground     → relay (port 5003) → channel → turbine
    """

    def __init__(
        self,
        bind_addr:    str   = LOCALHOST,
        bind_port:    int   = LEO_RELAY_PORT,
        turbine_addr: str   = LOCALHOST,
        turbine_port: int   = TURBINE_SENSOR_PORT,
        ground_addr:  str   = LOCALHOST,
        ground_port:  int   = GROUND_CONTROL_PORT,
        time_scale:   float = 1.0,
    ):
        self._bind         = (bind_addr,    bind_port)
        self._turbine      = (turbine_addr, turbine_port)
        self._ground       = (ground_addr,  ground_port)
        self._channel      = LEOChannel(time_scale=time_scale, apply_bandwidth_delay=True)
        self._running      = False

        # Blackout queues — (enqueue_time, raw_bytes, dest_addr)
        self._up_queue:   queue.Queue = queue.Queue()
        self._down_queue: queue.Queue = queue.Queue()

        # Raw UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(self._bind)
        logger.info(
            "[RELAY] Bound to %s:%d  turbine=%s:%d  ground=%s:%d",
            *self._bind, *self._turbine, *self._ground,
        )

    def start(self):
        self._running = True
        threading.Thread(target=self._recv_loop,   daemon=True, name="relay-rx").start()
        threading.Thread(target=self._queue_drain, args=(self._up_queue,   self._ground),   daemon=True, name="relay-uplink").start()
        threading.Thread(target=self._queue_drain, args=(self._down_queue, self._turbine),  daemon=True, name="relay-downlink").start()
        threading.Thread(target=self._stats_loop,  daemon=True, name="relay-stats").start()
        logger.info("[RELAY] Started")

    def stop(self):
        self._running = False
        self._sock.close()
        self._channel.stop()
        logger.info("[RELAY] Stopped")

    # ── Receive loop ──────────────────────────────────────────────────────────

    def _recv_loop(self):
        self._sock.settimeout(1.0)
        while self._running:
            try:
                raw, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            parsed = parse_frame(raw)
            if not parsed["valid"]:
                logger.debug("[RELAY] Dropped malformed frame from %s: %s", addr, parsed["error"])
                continue

            # Route by source address
            if addr[1] in (self._turbine[1],):
                # Uplink: turbine → ground
                logger.debug("[RELAY] Uplink from %s seq=%d type=0x%02X", addr, parsed["seq"], parsed["type"])
                self._enqueue(self._up_queue, raw, self._ground)
            else:
                # Downlink: ground → turbine
                logger.debug("[RELAY] Downlink from %s seq=%d type=0x%02X", addr, parsed["seq"], parsed["type"])
                self._enqueue(self._down_queue, raw, self._turbine)

    def _enqueue(self, q: queue.Queue, raw: bytes, dest: tuple):
        q.put((time.monotonic(), raw, dest))

    # ── Queue drain thread ────────────────────────────────────────────────────

    def _queue_drain(self, q: queue.Queue, dest: tuple):
        """
        Drain *q* through the channel model.
        During blackout, frames are held until the satellite becomes visible
        (or until they expire).
        """
        while self._running:
            try:
                enq_time, raw, _dest = q.get(timeout=0.5)
            except queue.Empty:
                continue

            age = time.monotonic() - enq_time
            if age > BLACKOUT_MAX_AGE_S:
                logger.debug("[RELAY] Discarding stale queued frame (age=%.1fs)", age)
                continue

            # If blackout, spin-wait in small increments
            while self._running and not self._channel.orbital_clock.is_visible():
                time.sleep(1.0)

            if not self._running:
                break

            # Apply channel model (latency, loss, bit errors)
            delivered = self._channel.transmit(raw, apply_latency=True)
            if delivered is None:
                logger.debug("[RELAY] Frame lost by channel model")
                continue

            try:
                self._sock.sendto(delivered, dest)
                logger.debug("[RELAY] Forwarded %d bytes → %s", len(delivered), dest)
            except OSError as exc:
                logger.error("[RELAY] Forward error: %s", exc)

    # ── Statistics logger ─────────────────────────────────────────────────────

    def _stats_loop(self):
        while self._running:
            time.sleep(30)
            stats = self._channel.get_stats()
            logger.info(
                "[RELAY] STATS visible=%s weather=%s loss_rate=%.2f%% "
                "doppler=%.0fHz next_pass=%.0fs up_q=%d dn_q=%d",
                stats["visible"],
                stats["weather"],
                stats["loss_rate"] * 100,
                stats["doppler_hz"],
                stats["next_pass_s"],
                self._up_queue.qsize(),
                self._down_queue.qsize(),
            )


def main():
    parser = argparse.ArgumentParser(description="TWCP LEO Satellite Relay")
    parser.add_argument("--bind",         default=LOCALHOST)
    parser.add_argument("--port",         type=int, default=LEO_RELAY_PORT)
    parser.add_argument("--turbine",      default=LOCALHOST)
    parser.add_argument("--turbine-port", type=int, default=TURBINE_SENSOR_PORT)
    parser.add_argument("--ground",       default=LOCALHOST)
    parser.add_argument("--ground-port",  type=int, default=GROUND_CONTROL_PORT)
    parser.add_argument(
        "--time-scale", type=float, default=60.0,
        help="Speed up orbital simulation (default: 60× = 1 min/s)"
    )
    args = parser.parse_args()

    relay = LEORelay(
        bind_addr    = args.bind,
        bind_port    = args.port,
        turbine_addr = args.turbine,
        turbine_port = args.turbine_port,
        ground_addr  = args.ground,
        ground_port  = args.ground_port,
        time_scale   = args.time_scale,
    )
    relay.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        relay.stop()
        logger.info("[RELAY] Exiting")


if __name__ == "__main__":
    main()
