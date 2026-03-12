"""
protocol/handshake.py — 4-step TWCP connection establishment.

Sequence:
  Initiator                         Responder
  ─────────                         ─────────
  DISCOVER  ──────────────────────►
            ◄──────────────────────  NEGOTIATE
  AGREE     ──────────────────────►
            ◄──────────────────────  ACK

After ACK the session is established and a shared session_id is known
to both sides.
"""

import hashlib
import json
import os
import socket
import struct
import threading
import time
import logging

from config import ARQ_TIMEOUT_S, ARQ_MAX_RETRIES, TWCP_VERSION
from protocol.frame import build_frame, parse_frame
from protocol.message_types import (
    MSG_DISCOVER,
    MSG_NEGOTIATE,
    MSG_AGREE,
    MSG_REJECT,
    MSG_ACK,
    MSG_NACK,
)

logger = logging.getLogger(__name__)


# ── Session state machine ─────────────────────────────────────────────────────

class SessionState:
    IDLE        = "IDLE"
    DISCOVERING = "DISCOVERING"
    NEGOTIATING = "NEGOTIATING"
    AGREED      = "AGREED"
    ESTABLISHED = "ESTABLISHED"
    REJECTED    = "REJECTED"


# ── Capability advertisement ──────────────────────────────────────────────────

DEFAULT_CAPABILITIES = {
    "protocol":          "TWCP",
    "version":           str(TWCP_VERSION),
    "sensor_types":      ["wind_speed", "yaw", "pitch", "rpm", "power", "temp", "vibration"],
    "commands":          ["YAW_CMD", "PITCH_CMD", "EMERGENCY_STOP"],
    "reliability":       ["STOP_AND_WAIT", "SELECTIVE_REPEAT"],
    "video":             False,
    "security":          True,
    "interop":           True,
}


def _make_session_id(local_id: str, remote_id: str) -> str:
    """Deterministic session ID from both node IDs + current time."""
    raw = f"{local_id}:{remote_id}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _build_discover(node_id: str, capabilities: dict | None = None) -> bytes:
    cap  = capabilities or DEFAULT_CAPABILITIES
    body = json.dumps({"node_id": node_id, "capabilities": cap}).encode()
    return build_frame(MSG_DISCOVER, 0, body)


def _build_negotiate(node_id: str, session_id: str, capabilities: dict | None = None) -> bytes:
    cap  = capabilities or DEFAULT_CAPABILITIES
    body = json.dumps({
        "node_id":    node_id,
        "session_id": session_id,
        "capabilities": cap,
    }).encode()
    return build_frame(MSG_NEGOTIATE, 0, body)


def _build_agree(node_id: str, session_id: str, agreed_caps: dict) -> bytes:
    body = json.dumps({
        "node_id":    node_id,
        "session_id": session_id,
        "agreed":     agreed_caps,
    }).encode()
    return build_frame(MSG_AGREE, 0, body)


def _build_ack(seq: int = 0) -> bytes:
    return build_frame(MSG_ACK, seq, struct.pack("!I", seq))


def _build_reject(reason: str) -> bytes:
    return build_frame(MSG_REJECT, 0, reason.encode())


def _negotiate_caps(mine: dict, theirs: dict) -> dict:
    """Compute the intersection of capabilities."""
    agreed = {}
    for k, v in mine.items():
        if k in theirs:
            if isinstance(v, list) and isinstance(theirs[k], list):
                agreed[k] = list(set(v) & set(theirs[k]))
            elif v == theirs[k]:
                agreed[k] = v
    return agreed


# ── Initiator (e.g., turbine side) ───────────────────────────────────────────

class HandshakeInitiator:
    """
    Performs the 4-step handshake from the initiating side.

    Usage::

        hs = HandshakeInitiator(sock, dest, "TURBINE-001")
        session = hs.connect()   # returns session dict or None
    """

    def __init__(
        self,
        sock: socket.socket,
        dest_addr: tuple,
        node_id: str,
        capabilities: dict | None = None,
        timeout: float = ARQ_TIMEOUT_S,
        max_retries: int = ARQ_MAX_RETRIES,
    ):
        self._sock    = sock
        self._dest    = dest_addr
        self._id      = node_id
        self._caps    = capabilities or dict(DEFAULT_CAPABILITIES)
        self._timeout = timeout
        self._retries = max_retries

    def connect(self) -> dict | None:
        """
        Execute the DISCOVER → NEGOTIATE → AGREE → ACK handshake.
        Returns session info dict on success, None on failure.
        """
        # Step 1: DISCOVER
        for attempt in range(1, self._retries + 1):
            disc = _build_discover(self._id, self._caps)
            self._sock.sendto(disc, self._dest)
            logger.info("[HS-INIT] DISCOVER sent (attempt %d)", attempt)

            response = self._recv_typed(MSG_NEGOTIATE)
            if response:
                break
            logger.warning("[HS-INIT] No NEGOTIATE received, retry %d", attempt)
        else:
            logger.error("[HS-INIT] Handshake failed — no NEGOTIATE")
            return None

        # Parse NEGOTIATE
        try:
            neg_data  = json.loads(response["payload"])
        except (json.JSONDecodeError, KeyError):
            logger.error("[HS-INIT] Malformed NEGOTIATE payload")
            return None

        peer_id    = neg_data.get("node_id", "UNKNOWN")
        # Use the session_id provided by the responder so both sides agree
        session_id = neg_data.get("session_id") or _make_session_id(self._id, peer_id)
        peer_caps  = neg_data.get("capabilities", {})
        agreed     = _negotiate_caps(self._caps, peer_caps)

        if not agreed:
            rej = _build_reject("No common capabilities")
            self._sock.sendto(rej, self._dest)
            logger.error("[HS-INIT] Rejected — no common capabilities")
            return None

        # Step 3: AGREE
        agree = _build_agree(self._id, session_id, agreed)
        self._sock.sendto(agree, self._dest)
        logger.info("[HS-INIT] AGREE sent session_id=%s", session_id)

        # Step 4: wait for ACK
        ack = self._recv_typed(MSG_ACK)
        if not ack:
            logger.error("[HS-INIT] No ACK received")
            return None

        logger.info("[HS-INIT] Session established session_id=%s", session_id)
        return {
            "session_id": session_id,
            "peer_id":    peer_id,
            "agreed":     agreed,
        }

    def _recv_typed(self, expected_type: int) -> dict | None:
        self._sock.settimeout(self._timeout)
        try:
            data, _ = self._sock.recvfrom(4096)
        except socket.timeout:
            return None
        finally:
            self._sock.settimeout(None)

        parsed = parse_frame(data)
        if not parsed["valid"]:
            logger.warning("[HS-INIT] Invalid frame: %s", parsed["error"])
            return None
        if parsed["type"] != expected_type:
            logger.warning(
                "[HS-INIT] Expected type 0x%02X got 0x%02X",
                expected_type, parsed["type"],
            )
            return None
        return parsed


# ── Responder (e.g., ground / relay side) ────────────────────────────────────

class HandshakeResponder:
    """
    Handles the responder side of the 4-step handshake.

    Usage::

        resp = HandshakeResponder("GROUND-001")
        session = resp.handle(raw_frame, src_addr, sock)
    """

    def __init__(
        self,
        node_id: str,
        capabilities: dict | None = None,
    ):
        self._id   = node_id
        self._caps = capabilities or dict(DEFAULT_CAPABILITIES)

    def handle_discover(self, frame: dict, src_addr: tuple, sock: socket.socket) -> dict | None:
        """
        Process an incoming DISCOVER frame and send back NEGOTIATE.
        Returns session context or None on error.
        """
        if frame["type"] != MSG_DISCOVER:
            return None
        try:
            disc_data = json.loads(frame["payload"])
        except json.JSONDecodeError:
            return None

        peer_id    = disc_data.get("node_id", "UNKNOWN")
        peer_caps  = disc_data.get("capabilities", {})
        session_id = _make_session_id(self._id, peer_id)
        agreed     = _negotiate_caps(self._caps, peer_caps)

        if not agreed:
            rej = _build_reject("No common capabilities")
            sock.sendto(rej, src_addr)
            logger.warning("[HS-RESP] Rejected %s — no common caps", peer_id)
            return None

        neg = _build_negotiate(self._id, session_id, self._caps)
        sock.sendto(neg, src_addr)
        logger.info("[HS-RESP] NEGOTIATE sent to %s session_id=%s", peer_id, session_id)

        return {
            "session_id": session_id,
            "peer_id":    peer_id,
            "agreed":     agreed,
            "state":      SessionState.NEGOTIATING,
            "src_addr":   src_addr,
        }

    def handle_agree(self, frame: dict, src_addr: tuple, sock: socket.socket, session: dict) -> bool:
        """
        Process an incoming AGREE frame and send final ACK.
        Returns True on success.
        """
        if frame["type"] != MSG_AGREE:
            return False
        try:
            agree_data = json.loads(frame["payload"])
        except json.JSONDecodeError:
            return False

        if agree_data.get("session_id") != session.get("session_id"):
            logger.warning("[HS-RESP] Session ID mismatch in AGREE")
            return False

        ack = _build_ack()
        sock.sendto(ack, src_addr)
        session["state"] = SessionState.ESTABLISHED
        logger.info(
            "[HS-RESP] ACK sent — session %s ESTABLISHED",
            session.get("session_id"),
        )
        return True
