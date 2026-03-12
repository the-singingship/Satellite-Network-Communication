"""
protocol/reliability.py — Custom ARQ reliability layer for TWCP.

Implements:
  - Stop-and-Wait ARQ     — for control commands (single outstanding frame)
  - Selective Repeat ARQ  — for sensor data streams (sliding window)
"""

import threading
import time
import logging

from config import ARQ_TIMEOUT_S, ARQ_MAX_RETRIES, SR_WINDOW_SIZE
from protocol.frame import build_frame, parse_frame
from protocol.message_types import MSG_ACK, MSG_NACK, FLAG_RELIABLE

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Stop-and-Wait ARQ
# ─────────────────────────────────────────────────────────────────────────────

class StopAndWaitARQ:
    """
    Reliable, ordered delivery over an unreliable socket using Stop-and-Wait.

    The sender transmits one frame at a time and blocks until an ACK (or
    NACK) is received, or until it exhausts all retries.

    Thread safety: only one concurrent send is supported.
    """

    def __init__(self, sock, dest_addr, timeout: float = ARQ_TIMEOUT_S, max_retries: int = ARQ_MAX_RETRIES):
        self._sock       = sock
        self._dest       = dest_addr
        self._timeout    = timeout
        self._max_retry  = max_retries
        self._seq        = 0
        self._lock       = threading.Lock()
        self._ack_event  = threading.Event()
        self._last_ack   = None   # (seq, success)

    # ── sender side ──────────────────────────────────────────────────────────

    def send_reliable(self, msg_type: int, payload: bytes) -> bool:
        """
        Send a frame and wait for ACK.  Returns True on success.
        Must be called from a single sender thread (or protected externally).
        """
        with self._lock:
            seq = self._seq

            for attempt in range(1, self._max_retry + 1):
                frame = build_frame(msg_type, seq, payload, flags=FLAG_RELIABLE)
                try:
                    self._sock.sendto(frame, self._dest)
                except OSError as exc:
                    logger.error("[SAW] sendto failed: %s", exc)
                    return False

                logger.debug(
                    "[SAW] Sent type=0x%02X seq=%d attempt=%d/%d",
                    msg_type, seq, attempt, self._max_retry,
                )

                self._ack_event.clear()
                if self._ack_event.wait(timeout=self._timeout):
                    seq_ack, success = self._last_ack
                    if seq_ack == seq:
                        if success:
                            self._seq = (seq + 1) & 0xFFFFFFFF
                            return True
                        else:
                            logger.warning("[SAW] NACK received for seq=%d", seq)
                            # Retransmit immediately
                            continue

                logger.warning("[SAW] Timeout waiting for ACK seq=%d", seq)

            logger.error("[SAW] Giving up after %d retries", self._max_retry)
            return False

    def notify_ack(self, seq: int, success: bool = True):
        """
        Called by the receiver thread when an ACK / NACK arrives.
        """
        self._last_ack = (seq, success)
        self._ack_event.set()

    # ── receiver-side helper ──────────────────────────────────────────────────

    @staticmethod
    def make_ack(seq: int) -> bytes:
        """Build an ACK frame for the given sequence number."""
        import struct
        return build_frame(MSG_ACK, seq, struct.pack("!I", seq))

    @staticmethod
    def make_nack(seq: int) -> bytes:
        """Build a NACK frame for the given sequence number."""
        import struct
        return build_frame(MSG_NACK, seq, struct.pack("!I", seq))


# ─────────────────────────────────────────────────────────────────────────────
# Selective Repeat ARQ
# ─────────────────────────────────────────────────────────────────────────────

class SelectiveRepeatARQ:
    """
    Sliding-window Selective Repeat ARQ for high-throughput sensor streams.

    The sender maintains a window of SR_WINDOW_SIZE outstanding frames.
    The receiver buffers out-of-order frames and ACKs each individually.
    Missing frames are selectively retransmitted by the sender.

    Both sender and receiver state are managed in this single object so
    that the same instance can be used in a loopback/relay scenario.
    For production use, instantiate one per direction.
    """

    def __init__(
        self,
        sock,
        dest_addr,
        window_size: int = SR_WINDOW_SIZE,
        timeout: float    = ARQ_TIMEOUT_S,
        max_retries: int  = ARQ_MAX_RETRIES,
    ):
        self._sock         = sock
        self._dest         = dest_addr
        self._window       = window_size
        self._timeout      = timeout
        self._max_retry    = max_retries

        # Sender state
        self._send_base    = 0           # oldest unACKed seq
        self._next_seq     = 0           # next seq to use
        self._send_buf: dict[int, tuple[bytes, float, int]] = {}
        # seq → (frame_bytes, send_time, retry_count)

        # Receiver state
        self._recv_base    = 0           # next expected seq (in-order)
        self._recv_buf: dict[int, bytes] = {}   # seq → payload (buffered)
        self._delivered: list[bytes] = []        # in-order delivered payloads

        self._lock         = threading.Lock()
        self._deliver_lock = threading.Lock()
        self._ack_received: dict[int, bool] = {}

    # ── sender side ──────────────────────────────────────────────────────────

    def _window_has_space(self) -> bool:
        return (self._next_seq - self._send_base) < self._window

    def send(self, msg_type: int, payload: bytes) -> int | None:
        """
        Enqueue one frame in the send window.  Returns the assigned seq number
        or None if the window is full.
        """
        with self._lock:
            if not self._window_has_space():
                return None
            seq   = self._next_seq
            frame = build_frame(msg_type, seq, payload, flags=FLAG_RELIABLE)
            try:
                self._sock.sendto(frame, self._dest)
            except OSError as exc:
                logger.error("[SR] sendto failed: %s", exc)
                return None
            self._send_buf[seq] = (frame, time.monotonic(), 0)
            self._next_seq      = (self._next_seq + 1) & 0xFFFFFFFF
            logger.debug("[SR] Sent seq=%d window_used=%d", seq, self._next_seq - self._send_base)
            return seq

    def process_ack(self, seq: int):
        """Mark a frame as ACKed, advance the window base if possible."""
        with self._lock:
            self._ack_received[seq] = True
            self._send_buf.pop(seq, None)
            # Slide window
            while self._ack_received.get(self._send_base, False):
                del self._ack_received[self._send_base]
                self._send_base = (self._send_base + 1) & 0xFFFFFFFF
            logger.debug("[SR] ACK seq=%d send_base=%d", seq, self._send_base)

    def retransmit_expired(self):
        """
        Check for timed-out frames in the send window and retransmit them.
        Call this periodically from a timer thread.
        """
        now = time.monotonic()
        with self._lock:
            for seq, (frame, t_sent, retries) in list(self._send_buf.items()):
                if now - t_sent >= self._timeout:
                    if retries >= self._max_retry:
                        logger.error("[SR] Dropping seq=%d after %d retries", seq, retries)
                        del self._send_buf[seq]
                    else:
                        try:
                            self._sock.sendto(frame, self._dest)
                        except OSError as exc:
                            logger.error("[SR] Retransmit failed: %s", exc)
                        self._send_buf[seq] = (frame, now, retries + 1)
                        logger.debug("[SR] Retransmit seq=%d (attempt %d)", seq, retries + 1)

    # ── receiver side ─────────────────────────────────────────────────────────

    def receive(self, seq: int, payload: bytes) -> list[bytes]:
        """
        Process an incoming data frame at the receiver.
        Buffers out-of-order frames; returns a list of in-order payloads
        that can now be delivered to the application.
        """
        with self._deliver_lock:
            # Check within the receive window
            hi = (self._recv_base + self._window - 1) & 0xFFFFFFFF
            # Accept if within [recv_base, recv_base+window-1]
            if self._in_window(seq, self._recv_base, hi):
                if seq not in self._recv_buf:
                    self._recv_buf[seq] = payload
                # Deliver contiguous frames
                delivered = []
                while self._recv_base in self._recv_buf:
                    delivered.append(self._recv_buf.pop(self._recv_base))
                    self._recv_base = (self._recv_base + 1) & 0xFFFFFFFF
                self._delivered.extend(delivered)
                return delivered
            return []

    @staticmethod
    def _in_window(seq: int, base: int, hi: int) -> bool:
        """Handle wrap-around for 32-bit sequence numbers."""
        if base <= hi:
            return base <= seq <= hi
        # Wrapped
        return seq >= base or seq <= hi

    def make_ack(self, seq: int) -> bytes:
        import struct
        return build_frame(MSG_ACK, seq, struct.pack("!I", seq))
