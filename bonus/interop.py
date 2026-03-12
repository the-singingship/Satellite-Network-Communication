"""
bonus/interop.py — Bonus 2: Interoperation interface.

Defines a clean JSON-based capability advertisement format and a standard
discovery message structure for cross-group communication.

Other groups can implement their own protocol but use this schema to
exchange DISCOVER messages on Port 5005.
"""

import json
import socket
import time
import logging

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DISCOVERY_PORT, BROADCAST_ADDR, INTEROP_VERSION, INTEROP_PROTOCOL_NAME, TURBINE_ID
from utils.logger import get_logger

logger = get_logger(__name__)


# ── Schema version ─────────────────────────────────────────────────────────────

INTEROP_SCHEMA_VERSION = "1.0"

# ── Standard interop DISCOVER payload ────────────────────────────────────────

def build_interop_discover(
    node_id:        str,
    protocol_name:  str = INTEROP_PROTOCOL_NAME,
    protocol_ver:   str = INTEROP_VERSION,
    capabilities:   dict | None = None,
    listen_port:    int = DISCOVERY_PORT,
    group_name:     str = "CSU33D03-Group",
) -> dict:
    """
    Build a JSON-serialisable interoperation DISCOVER message.

    This is the *payload* that goes inside a TWCP DISCOVER frame (or any
    other group's outer frame) so that cross-group discovery works even
    when the outer framing differs.
    """
    caps = capabilities or {
        "sensor_data":    True,
        "yaw_cmd":        True,
        "pitch_cmd":      True,
        "emergency_stop": True,
        "heartbeat":      True,
        "video":          False,
        "security":       True,
    }
    return {
        "schema":        INTEROP_SCHEMA_VERSION,
        "protocol":      protocol_name,
        "version":       protocol_ver,
        "node_id":       node_id,
        "group":         group_name,
        "listen_port":   listen_port,
        "timestamp":     time.time(),
        "capabilities":  caps,
    }


def validate_interop_discover(msg: dict) -> tuple[bool, str]:
    """
    Validate a received interop DISCOVER message.
    Returns (ok, reason).
    """
    required = {"schema", "protocol", "version", "node_id", "capabilities"}
    missing  = required - set(msg.keys())
    if missing:
        return False, f"Missing fields: {missing}"
    if msg.get("schema") != INTEROP_SCHEMA_VERSION:
        return False, f"Unsupported schema version: {msg.get('schema')!r}"
    return True, "ok"


# ── Interop broadcaster ───────────────────────────────────────────────────────

class InteropBeacon:
    """
    Periodically broadcasts a plain-JSON DISCOVER message (no TWCP framing)
    to allow foreign nodes that don't know the TWCP frame format to still
    discover this node.

    The message is sent as a raw UDP datagram containing UTF-8 JSON.
    """

    def __init__(
        self,
        node_id:    str = TURBINE_ID,
        port:       int = DISCOVERY_PORT,
        interval_s: float = 30.0,
    ):
        self._node_id  = node_id
        self._port     = port
        self._interval = interval_s
        self._running  = False

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def start(self):
        import threading
        self._running = True
        threading.Thread(target=self._beacon_loop, daemon=True, name="interop-beacon").start()
        logger.info("[INTEROP] Beacon started node_id=%s", self._node_id)

    def stop(self):
        self._running = False
        self._sock.close()

    def _beacon_loop(self):
        while self._running:
            msg  = build_interop_discover(self._node_id)
            data = json.dumps(msg).encode()
            try:
                self._sock.sendto(data, (BROADCAST_ADDR, self._port))
                logger.debug("[INTEROP] Beacon broadcast %d bytes", len(data))
            except OSError as exc:
                logger.warning("[INTEROP] Beacon broadcast failed: %s", exc)
            time.sleep(self._interval)


# ── Interop listener ──────────────────────────────────────────────────────────

class InteropListener:
    """
    Listens for plain-JSON DISCOVER messages from foreign nodes.
    Calls *on_discover* for each valid message received.
    """

    def __init__(self, port: int = DISCOVERY_PORT, on_discover=None):
        self._port       = port
        self._on_discover = on_discover or self._default_handler
        self._running    = False

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bind to 0.0.0.0 intentionally: interop discovery messages arrive as
        # UDP broadcasts and must be received on all network interfaces.
        self._sock.bind(("0.0.0.0", port))

    def start(self):
        import threading
        self._running = True
        threading.Thread(target=self._recv_loop, daemon=True, name="interop-rx").start()
        logger.info("[INTEROP] Listener started on port %d", self._port)

    def stop(self):
        self._running = False
        self._sock.close()

    def _recv_loop(self):
        self._sock.settimeout(1.0)
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            # Try JSON decode (foreign node messages)
            try:
                msg = json.loads(data.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue   # Not a plain-JSON message; could be TWCP frame

            ok, reason = validate_interop_discover(msg)
            if ok:
                self._on_discover(msg, addr)
            else:
                logger.debug("[INTEROP] Invalid interop msg from %s: %s", addr, reason)

    @staticmethod
    def _default_handler(msg: dict, addr):
        logger.info(
            "[INTEROP] Foreign DISCOVER from %s  node_id=%s  protocol=%s/%s",
            addr, msg.get("node_id"), msg.get("protocol"), msg.get("version"),
        )
