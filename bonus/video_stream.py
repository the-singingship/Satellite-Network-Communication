"""
bonus/video_stream.py — Bonus 1: Simulated O&M video streaming.

Simulates bi-directional video data transmission using TWCP VIDEO_FRAME
messages.  Implements:
  - Frame fragmentation and reassembly for large "video" payloads
  - Adaptive bitrate based on current channel conditions
  - Fragment sequence tracking and reorder buffer
"""

import json
import math
import os
import socket
import struct
import threading
import time
import logging

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.frame import build_frame, parse_frame
from protocol.message_types import (
    MSG_VIDEO_FRAME,
    FLAG_FRAGMENTED,
    FLAG_LAST_FRAG,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_FRAGMENT_BYTES = 1024       # max payload per TWCP fragment
BASE_BITRATE_KBPS  = 512        # default video bitrate (kbps)
MIN_BITRATE_KBPS   = 128
MAX_BITRATE_KBPS   = 2048
FRAME_HEADER_FMT   = "!IHHH"   # video_id(4), frag_idx(2), total_frags(2), frame_w(2)
FRAME_HEADER_BYTES = struct.calcsize(FRAME_HEADER_FMT)


# ── Adaptive bitrate controller ───────────────────────────────────────────────

class AdaptiveBitrateController:
    """
    Adjusts simulated video bitrate based on observed channel loss rate.
    """

    def __init__(self):
        self._bitrate_kbps = BASE_BITRATE_KBPS
        self._lock         = threading.Lock()

    def update(self, loss_rate: float):
        """
        Decrease bitrate on high loss; increase on low loss.
        loss_rate ∈ [0, 1].
        """
        with self._lock:
            if loss_rate > 0.15:
                self._bitrate_kbps = max(MIN_BITRATE_KBPS, int(self._bitrate_kbps * 0.7))
            elif loss_rate < 0.03:
                self._bitrate_kbps = min(MAX_BITRATE_KBPS, int(self._bitrate_kbps * 1.2))
        logger.debug("[ABR] Bitrate → %d kbps (loss=%.1f%%)", self._bitrate_kbps, loss_rate * 100)

    @property
    def bitrate_kbps(self) -> int:
        with self._lock:
            return self._bitrate_kbps

    def frame_size_bytes(self) -> int:
        """Target frame payload size at the current bitrate (assume 10 fps)."""
        return (self._bitrate_kbps * 1000) // (8 * 10)


# ── Video Frame Sender ────────────────────────────────────────────────────────

class VideoStreamSender:
    """
    Generates synthetic "video frames" and sends them via TWCP VIDEO_FRAME
    messages with fragmentation.
    """

    def __init__(
        self,
        sock:    socket.socket,
        dest:    tuple,
        abr:     AdaptiveBitrateController | None = None,
    ):
        self._sock      = sock
        self._dest      = dest
        self._abr       = abr or AdaptiveBitrateController()
        self._video_id  = 0
        self._seq       = 0
        self._running   = False

    def start(self):
        self._running = True
        threading.Thread(target=self._stream_loop, daemon=True, name="video-tx").start()

    def stop(self):
        self._running = False

    def _stream_loop(self):
        fps = 10
        while self._running:
            frame_data = self._synthesise_frame()
            self._send_frame(frame_data)
            self._video_id += 1
            time.sleep(1.0 / fps)

    def _synthesise_frame(self) -> bytes:
        """Generate a fake compressed video frame (random bytes + metadata)."""
        target_size = self._abr.frame_size_bytes()
        meta = json.dumps({
            "video_id":  self._video_id,
            "timestamp": time.time(),
            "width":     1280,
            "height":    720,
            "codec":     "TWCP-VIDEO/1",
        }).encode()
        padding = os.urandom(max(0, target_size - len(meta)))
        return meta + padding

    def _send_frame(self, frame_data: bytes):
        """Fragment *frame_data* and send via TWCP."""
        total_frags = math.ceil(len(frame_data) / MAX_FRAGMENT_BYTES)
        for idx in range(total_frags):
            chunk     = frame_data[idx * MAX_FRAGMENT_BYTES : (idx + 1) * MAX_FRAGMENT_BYTES]
            is_last   = (idx == total_frags - 1)
            vid_hdr   = struct.pack(FRAME_HEADER_FMT, self._video_id, idx, total_frags, 1280)
            payload   = vid_hdr + chunk
            flags     = FLAG_FRAGMENTED | (FLAG_LAST_FRAG if is_last else 0)
            twcp_frame = build_frame(MSG_VIDEO_FRAME, self._seq, payload, flags=flags)
            self._seq  = (self._seq + 1) & 0xFFFFFFFF
            try:
                self._sock.sendto(twcp_frame, self._dest)
            except OSError as exc:
                logger.error("[VIDEO-TX] Send error: %s", exc)

        logger.debug(
            "[VIDEO-TX] Sent video_id=%d  %d frags  %d bytes",
            self._video_id, total_frags, len(frame_data),
        )


# ── Video Frame Receiver ──────────────────────────────────────────────────────

class VideoStreamReceiver:
    """
    Receives fragmented TWCP VIDEO_FRAME messages and reassembles them.
    Delivers complete frames to a callback.
    """

    def __init__(self, on_frame=None):
        self._buf:      dict[int, dict] = {}   # video_id → {idx: bytes, total}
        self._lock      = threading.Lock()
        self._on_frame  = on_frame or self._default_on_frame
        self._frames_ok = 0
        self._frags_ok  = 0

    def receive(self, frame: dict):
        """Process one parsed TWCP VIDEO_FRAME dict."""
        if frame["type"] != MSG_VIDEO_FRAME:
            return
        payload = frame["payload"]
        if len(payload) < FRAME_HEADER_BYTES:
            return

        video_id, frag_idx, total_frags, _ = struct.unpack_from(FRAME_HEADER_FMT, payload)
        chunk = payload[FRAME_HEADER_BYTES:]

        with self._lock:
            if video_id not in self._buf:
                self._buf[video_id] = {"total": total_frags, "frags": {}}
            entry = self._buf[video_id]
            entry["frags"][frag_idx] = chunk
            self._frags_ok += 1

            # Check if we have all fragments inside the lock to avoid race conditions
            complete = len(entry["frags"]) == entry["total"]
            if complete:
                assembled = b"".join(entry["frags"][i] for i in range(entry["total"]))
                del self._buf[video_id]
                self._frames_ok += 1

        if complete:
            self._on_frame(video_id, assembled)

    @staticmethod
    def _default_on_frame(video_id: int, data: bytes):
        logger.info(
            "[VIDEO-RX] Frame %d reassembled  %d bytes",
            video_id, len(data),
        )

    @property
    def stats(self) -> dict:
        return {"frames_ok": self._frames_ok, "frags_ok": self._frags_ok}
