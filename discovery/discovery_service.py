"""
discovery/discovery_service.py — Process 5: Discovery and Negotiation Service.

Transport: UDP broadcast, Port 5005
Discovers proximate wind turbines on the network.
Negotiates capabilities and establishes agreements.
Supports interoperation with other groups' implementations.
"""

import json
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
    DISCOVERY_PORT,
    BROADCAST_ADDR,
    LOCALHOST,
    TURBINE_ID,
    INTEROP_VERSION,
    INTEROP_PROTOCOL_NAME,
)
from protocol.frame import build_frame, parse_frame
from protocol.message_types import (
    MSG_DISCOVER,
    MSG_NEGOTIATE,
    MSG_AGREE,
    MSG_REJECT,
    MSG_ACK,
)
from protocol.handshake import (
    DEFAULT_CAPABILITIES,
    HandshakeResponder,
    _make_session_id,
    _negotiate_caps,
)
from discovery.negotiation import (
    build_negotiate_payload,
    build_agree_payload,
    build_reject_payload,
    negotiate_capabilities,
)
from utils.logger import setup_logging

logger = setup_logging("discovery_service")

# How often to broadcast our presence
ANNOUNCE_INTERVAL_S = 15


class DiscoveryService:
    """
    Broadcasts DISCOVER frames and handles incoming DISCOVER / NEGOTIATE /
    AGREE messages from peer turbines or foreign (interop) nodes.

    Maintains a registry of known peers and their agreed capabilities.
    """

    def __init__(
        self,
        bind_addr:  str = "0.0.0.0",
        bind_port:  int = DISCOVERY_PORT,
        node_id:    str = TURBINE_ID,
        capabilities: dict | None = None,
    ):
        self._bind    = (bind_addr, bind_port)
        self._node_id = node_id
        self._caps    = capabilities or dict(DEFAULT_CAPABILITIES)
        self._running = False
        self._seq     = 0

        # Registry: node_id → session info
        self._peers: dict[str, dict] = {}
        self._peers_lock = threading.Lock()

        # Pending negotiations: session_id → context
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # Broadcast socket — intentionally bound to 0.0.0.0 so that incoming
        # UDP broadcast datagrams (255.255.255.255) are received on all
        # available network interfaces.  This is required for LAN-wide
        # turbine discovery and is the standard pattern for broadcast servers.
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind(self._bind)
        logger.info("[DISC] Bound to %s:%d node_id=%s", *self._bind, self._node_id)

    def start(self):
        self._running = True
        threading.Thread(target=self._recv_loop,     daemon=True, name="disc-rx").start()
        threading.Thread(target=self._announce_loop, daemon=True, name="disc-tx").start()
        logger.info("[DISC] Started")

    def stop(self):
        self._running = False
        self._sock.close()
        logger.info("[DISC] Stopped")

    # ── Announce loop ─────────────────────────────────────────────────────────

    def _announce_loop(self):
        while self._running:
            self._broadcast_discover()
            time.sleep(ANNOUNCE_INTERVAL_S)

    def _broadcast_discover(self):
        payload = json.dumps({
            "node_id":      self._node_id,
            "capabilities": self._caps,
            "interop": {
                "protocol": INTEROP_PROTOCOL_NAME,
                "version":  INTEROP_VERSION,
            },
        }).encode()
        frame = build_frame(MSG_DISCOVER, self._seq, payload)
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        try:
            self._sock.sendto(frame, (BROADCAST_ADDR, DISCOVERY_PORT))
            logger.debug("[DISC] Broadcast DISCOVER seq=%d", self._seq - 1)
        except OSError as exc:
            logger.warning("[DISC] Broadcast failed: %s", exc)

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
                logger.debug("[DISC] Bad frame from %s", addr)
                continue

            mtype = parsed["type"]
            if mtype == MSG_DISCOVER:
                self._handle_discover(parsed, addr)
            elif mtype == MSG_NEGOTIATE:
                self._handle_negotiate(parsed, addr)
            elif mtype == MSG_AGREE:
                self._handle_agree(parsed, addr)
            elif mtype == MSG_REJECT:
                self._handle_reject(parsed, addr)
            else:
                logger.debug("[DISC] Unhandled type 0x%02X from %s", mtype, addr)

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _handle_discover(self, frame: dict, addr):
        try:
            data = json.loads(frame["payload"])
        except json.JSONDecodeError:
            return

        peer_id   = data.get("node_id", "UNKNOWN")
        peer_caps = data.get("capabilities", {})

        # Don't respond to our own broadcast
        if peer_id == self._node_id:
            return

        logger.info("[DISC] DISCOVER from %s  addr=%s", peer_id, addr)

        # Compute agreed capabilities
        agreed = negotiate_capabilities(self._caps, peer_caps)
        if not agreed:
            rej = build_frame(MSG_REJECT, 0, build_reject_payload("No common capabilities"))
            self._sock.sendto(rej, addr)
            logger.warning("[DISC] Rejected %s — no common capabilities", peer_id)
            return

        session_id = _make_session_id(self._node_id, peer_id)

        # Send NEGOTIATE
        neg_payload = build_negotiate_payload(self._node_id, session_id, self._caps)
        neg_frame   = build_frame(MSG_NEGOTIATE, self._seq, neg_payload)
        self._seq   = (self._seq + 1) & 0xFFFFFFFF
        self._sock.sendto(neg_frame, addr)
        logger.info("[DISC] NEGOTIATE sent to %s session_id=%s", peer_id, session_id)

        with self._pending_lock:
            self._pending[session_id] = {
                "peer_id":    peer_id,
                "addr":       addr,
                "agreed":     agreed,
                "session_id": session_id,
                "started_at": time.time(),
            }

    def _handle_negotiate(self, frame: dict, addr):
        try:
            data = json.loads(frame["payload"])
        except json.JSONDecodeError:
            return

        peer_id    = data.get("node_id", "UNKNOWN")
        session_id = data.get("session_id", "")
        peer_caps  = data.get("capabilities", {})

        if peer_id == self._node_id:
            return

        logger.info("[DISC] NEGOTIATE from %s session=%s", peer_id, session_id)

        agreed = negotiate_capabilities(self._caps, peer_caps)
        if not agreed:
            rej = build_frame(MSG_REJECT, 0, build_reject_payload("No capabilities to agree on"))
            self._sock.sendto(rej, addr)
            return

        # Send AGREE
        agree_payload = build_agree_payload(self._node_id, session_id, agreed)
        agree_frame   = build_frame(MSG_AGREE, self._seq, agree_payload)
        self._seq     = (self._seq + 1) & 0xFFFFFFFF
        self._sock.sendto(agree_frame, addr)
        logger.info("[DISC] AGREE sent to %s agreed=%s", peer_id, list(agreed.keys()))

    def _handle_agree(self, frame: dict, addr):
        try:
            data = json.loads(frame["payload"])
        except json.JSONDecodeError:
            return

        session_id = data.get("session_id", "")
        peer_id    = data.get("node_id", "UNKNOWN")
        agreed     = data.get("agreed", {})

        logger.info("[DISC] AGREE from %s session=%s caps=%s", peer_id, session_id, list(agreed.keys()))

        # Send ACK
        ack = build_frame(MSG_ACK, frame["seq"], struct.pack("!I", frame["seq"]))
        self._sock.sendto(ack, addr)

        # Register peer
        with self._peers_lock:
            self._peers[peer_id] = {
                "session_id":    session_id,
                "addr":          addr,
                "agreed":        agreed,
                "established_at": time.time(),
            }
        logger.info("[DISC] Session ESTABLISHED with %s", peer_id)

        # Remove from pending
        with self._pending_lock:
            self._pending.pop(session_id, None)

    def _handle_reject(self, frame: dict, addr):
        try:
            data = json.loads(frame["payload"])
        except json.JSONDecodeError:
            data = {}
        logger.warning("[DISC] REJECT from %s reason=%s", addr, data.get("reason", "?"))

    # ── Public API ────────────────────────────────────────────────────────────

    def get_peers(self) -> dict:
        with self._peers_lock:
            return dict(self._peers)

    def get_peer_count(self) -> int:
        with self._peers_lock:
            return len(self._peers)


def main():
    parser = argparse.ArgumentParser(description="TWCP Discovery and Negotiation Service")
    parser.add_argument("--bind",    default="0.0.0.0")
    parser.add_argument("--port",    type=int, default=DISCOVERY_PORT)
    parser.add_argument("--node-id", default=TURBINE_ID)
    args = parser.parse_args()

    svc = DiscoveryService(
        bind_addr = args.bind,
        bind_port = args.port,
        node_id   = args.node_id,
    )
    svc.start()

    try:
        while True:
            time.sleep(10)
            peers = svc.get_peers()
            if peers:
                logger.info("[DISC] Known peers: %s", list(peers.keys()))
    except KeyboardInterrupt:
        svc.stop()
        logger.info("[DISC] Exiting")


if __name__ == "__main__":
    main()
