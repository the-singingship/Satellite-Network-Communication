"""
bonus/security.py — Bonus 3: Malicious behaviour detection and simple security.

Features:
  - Challenge-response authentication (HMAC-SHA256)
  - Replay attack detection using sequence number + timestamp window
  - Anomalous command detection (out-of-range values, impossible rates)
  - Rate limiting (per-source command counting)
  - Logging of all suspicious activity
"""

import hashlib
import hmac
import json
import os
import struct
import threading
import time
import logging

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CHALLENGE_LENGTH, REPLAY_WINDOW_S, RATE_LIMIT_CMDS_PER_S
from protocol.frame import build_frame, parse_frame
from protocol.message_types import MSG_KEY_EXCHANGE, MSG_REJECT
from utils.logger import get_logger

logger = get_logger(__name__)


# ── HMAC challenge-response ───────────────────────────────────────────────────

class ChallengeAuth:
    """
    Simple HMAC-SHA256 challenge-response authentication.

    Both sides share a pre-shared key (PSK).
    The authenticator sends a random challenge; the peer must reply with
    HMAC(PSK, challenge).
    """

    def __init__(self, psk: bytes | None = None):
        # Default PSK for demo — in production this must be securely exchanged
        self._psk = psk or b"TWCP-DEMO-PSK-2025"

    def generate_challenge(self) -> bytes:
        """Return CHALLENGE_LENGTH random bytes."""
        return os.urandom(CHALLENGE_LENGTH)

    def compute_response(self, challenge: bytes) -> bytes:
        """Compute HMAC-SHA256(PSK, challenge)."""
        return hmac.new(self._psk, challenge, hashlib.sha256).digest()

    def verify_response(self, challenge: bytes, response: bytes) -> bool:
        """Constant-time comparison of expected vs received response."""
        expected = self.compute_response(challenge)
        return hmac.compare_digest(expected, response)

    def build_key_exchange(self, challenge: bytes, response: bytes | None = None) -> bytes:
        """
        Build a KEY_EXCHANGE frame.
        If *response* is None, this is the challenge frame.
        If *response* is set, this is the response frame.
        """
        payload = json.dumps({
            "challenge": challenge.hex(),
            "response":  response.hex() if response else None,
        }).encode()
        return build_frame(MSG_KEY_EXCHANGE, 0, payload)


# ── Replay detection ──────────────────────────────────────────────────────────

class ReplayDetector:
    """
    Tracks seen (source, seq) pairs within a sliding time window.
    Flags any duplicate as a replay attack.
    """

    def __init__(self, window_s: float = REPLAY_WINDOW_S):
        self._window = window_s
        self._seen:  dict[tuple, float] = {}   # (src, seq) → first_seen_time
        self._lock   = threading.Lock()

    def check(self, src: str, seq: int) -> bool:
        """
        Returns True if this (src, seq) is fresh (not a replay).
        Side-effect: records the pair.
        """
        now = time.monotonic()
        key = (src, seq)
        with self._lock:
            # Prune expired entries
            expired = [k for k, t in self._seen.items() if now - t > self._window]
            for k in expired:
                del self._seen[k]

            if key in self._seen:
                logger.warning(
                    "[SECURITY] REPLAY DETECTED src=%s seq=%d (first seen %.1fs ago)",
                    src, seq, now - self._seen[key],
                )
                return False   # Replay!
            self._seen[key] = now
            return True


# ── Anomaly detector ──────────────────────────────────────────────────────────

class AnomalyDetector:
    """
    Detects anomalous control commands based on physics-plausible limits
    and rate-of-change checks.
    """

    # Hard limits
    YAW_MIN,   YAW_MAX   =   0.0, 360.0
    PITCH_MIN, PITCH_MAX =   0.0,  90.0
    MAX_YAW_RATE_DEG_S   =  10.0   # physically impossible to change faster
    MAX_PITCH_RATE_DEG_S =  15.0

    def __init__(self):
        self._last_yaw:   dict[str, tuple[float, float]] = {}  # src → (angle, time)
        self._last_pitch: dict[str, tuple[float, float]] = {}
        self._lock        = threading.Lock()
        self._alerts:     list[dict] = []
        self._alerts_lock = threading.Lock()

    def check_yaw(self, src: str, angle: float) -> bool:
        """Returns True if the yaw command is plausible."""
        return self._check_angle(
            src, angle, self._last_yaw,
            self.YAW_MIN, self.YAW_MAX, self.MAX_YAW_RATE_DEG_S, "YAW"
        )

    def check_pitch(self, src: str, angle: float) -> bool:
        """Returns True if the pitch command is plausible."""
        return self._check_angle(
            src, angle, self._last_pitch,
            self.PITCH_MIN, self.PITCH_MAX, self.MAX_PITCH_RATE_DEG_S, "PITCH"
        )

    def _check_angle(
        self,
        src:      str,
        angle:    float,
        history:  dict,
        lo:       float,
        hi:       float,
        max_rate: float,
        label:    str,
    ) -> bool:
        now = time.monotonic()
        ok  = True

        if not (lo <= angle <= hi):
            self._alert(src, f"{label} out of range: {angle:.1f} not in [{lo}, {hi}]")
            ok = False

        with self._lock:
            if src in history:
                prev_angle, prev_time = history[src]
                dt = now - prev_time
                if dt > 0:
                    rate = abs(angle - prev_angle) / dt
                    if rate > max_rate:
                        self._alert(
                            src,
                            f"{label} rate anomaly: {rate:.1f}°/s > {max_rate}°/s"
                        )
                        ok = False
            history[src] = (angle, now)

        return ok

    def _alert(self, src: str, reason: str):
        entry = {"src": src, "reason": reason, "ts": time.time()}
        logger.warning("[SECURITY] ANOMALY from %s: %s", src, reason)
        with self._alerts_lock:
            self._alerts.append(entry)

    def get_alerts(self) -> list[dict]:
        with self._alerts_lock:
            return list(self._alerts)


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Token-bucket style rate limiter (per source address).
    """

    def __init__(self, max_per_second: float = RATE_LIMIT_CMDS_PER_S):
        self._max  = max_per_second
        self._hist: dict[str, list[float]] = {}
        self._lock  = threading.Lock()

    def allow(self, src: str) -> bool:
        """Returns True if *src* is within its rate budget."""
        now = time.monotonic()
        with self._lock:
            ts = self._hist.setdefault(src, [])
            ts[:] = [t for t in ts if now - t < 1.0]
            if len(ts) >= self._max:
                logger.warning(
                    "[SECURITY] RATE LIMIT exceeded for %s (%d cmd/s)",
                    src, len(ts),
                )
                return False
            ts.append(now)
            return True


# ── Combined security gate ────────────────────────────────────────────────────

class SecurityGate:
    """
    Wraps replay detection, anomaly detection, and rate limiting
    into a single check for each incoming command frame.
    """

    def __init__(self):
        self.replay   = ReplayDetector()
        self.anomaly  = AnomalyDetector()
        self.ratelim  = RateLimiter()

    def check_frame(self, src: str, frame: dict) -> bool:
        """
        Return True if the frame passes all security checks.
        A return value of False means the frame should be dropped.
        """
        from protocol.message_types import MSG_YAW_CMD, MSG_PITCH_CMD

        # 1. Rate limit
        if not self.ratelim.allow(src):
            return False

        # 2. Replay detection
        if not self.replay.check(src, frame.get("seq", 0)):
            return False

        # 3. Anomaly detection (for control commands only)
        mtype = frame.get("type", 0)
        payload = frame.get("payload", b"")

        if mtype in (MSG_YAW_CMD, MSG_PITCH_CMD):
            try:
                cmd   = json.loads(payload)
                angle = float(cmd["angle_deg"])
            except Exception:
                logger.warning("[SECURITY] Unparseable command from %s", src)
                return False

            if mtype == MSG_YAW_CMD:
                if not self.anomaly.check_yaw(src, angle):
                    return False
            else:
                if not self.anomaly.check_pitch(src, angle):
                    return False

        return True
