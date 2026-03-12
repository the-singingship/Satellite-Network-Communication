"""
turbine/actuator_controller.py — Process 2: Turbine Actuator Controller.

Transport: TCP raw socket, Port 5002
Receives yaw/pitch control commands from the ground station (via relay).
Simulates actuator responses with realistic delays.
Implements command validation and safety limits.
Reports actuator status back to the sender.
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
    TURBINE_ACTUATOR_PORT,
    LOCALHOST,
    TURBINE_ID,
    RATE_LIMIT_CMDS_PER_S,
)
from protocol.frame import build_frame, parse_frame
from protocol.message_types import (
    MSG_YAW_CMD,
    MSG_PITCH_CMD,
    MSG_EMERGENCY_STOP,
    MSG_ACK,
    MSG_NACK,
    MSG_STATUS,
    FLAG_RELIABLE,
)
from utils.logger import setup_logging

logger = setup_logging("actuator_controller")


# ── Safety constants ──────────────────────────────────────────────────────────

YAW_MIN_DEG      =   0.0
YAW_MAX_DEG      = 360.0
PITCH_MIN_DEG    =   0.0
PITCH_MAX_DEG    =  90.0
YAW_RATE_DEG_S   =   3.0   # max yaw rate
PITCH_RATE_DEG_S =   5.0   # max pitch rate
ACTUATOR_DELAY_S =   0.5   # realistic actuator response time


# ── Actuator state ────────────────────────────────────────────────────────────

class ActuatorState:
    def __init__(self):
        self.yaw_actual   = 180.0
        self.pitch_actual = 10.0
        self.yaw_target   = 180.0
        self.pitch_target = 10.0
        self.e_stop       = False
        self.lock         = threading.Lock()

    def apply_yaw(self, angle: float) -> dict:
        """Validate and stage a new yaw command. Returns status dict."""
        if not (YAW_MIN_DEG <= angle <= YAW_MAX_DEG):
            return {"ok": False, "reason": f"Yaw {angle} out of range [{YAW_MIN_DEG}, {YAW_MAX_DEG}]"}
        with self.lock:
            if self.e_stop:
                return {"ok": False, "reason": "Emergency stop active"}
            self.yaw_target = angle
        return {"ok": True, "yaw_target": angle}

    def apply_pitch(self, angle: float) -> dict:
        """Validate and stage a new pitch command. Returns status dict."""
        if not (PITCH_MIN_DEG <= angle <= PITCH_MAX_DEG):
            return {"ok": False, "reason": f"Pitch {angle} out of range [{PITCH_MIN_DEG}, {PITCH_MAX_DEG}]"}
        with self.lock:
            if self.e_stop:
                return {"ok": False, "reason": "Emergency stop active"}
            self.pitch_target = angle
        return {"ok": True, "pitch_target": angle}

    def emergency_stop(self) -> dict:
        with self.lock:
            self.e_stop       = True
            self.pitch_target = PITCH_MAX_DEG   # feather blades
        logger.critical("[ACTUATOR] EMERGENCY STOP engaged")
        return {"ok": True, "e_stop": True}

    def tick(self, dt: float = 0.1):
        """Move actuators toward targets at physical rate."""
        with self.lock:
            if self.e_stop:
                self.pitch_actual = min(PITCH_MAX_DEG, self.pitch_actual + PITCH_RATE_DEG_S * dt)
                return

            # Yaw
            yaw_err = (self.yaw_target - self.yaw_actual + 180) % 360 - 180
            move    = max(-YAW_RATE_DEG_S * dt, min(YAW_RATE_DEG_S * dt, yaw_err))
            self.yaw_actual = (self.yaw_actual + move) % 360

            # Pitch
            pitch_err = self.pitch_target - self.pitch_actual
            move_p    = max(-PITCH_RATE_DEG_S * dt, min(PITCH_RATE_DEG_S * dt, pitch_err))
            self.pitch_actual = max(PITCH_MIN_DEG, min(PITCH_MAX_DEG, self.pitch_actual + move_p))

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "turbine_id":    TURBINE_ID,
                "timestamp":     time.time(),
                "yaw_actual":    round(self.yaw_actual,   2),
                "yaw_target":    round(self.yaw_target,   2),
                "pitch_actual":  round(self.pitch_actual, 2),
                "pitch_target":  round(self.pitch_target, 2),
                "e_stop":        self.e_stop,
            }


# ── Actuator Controller server ────────────────────────────────────────────────

class ActuatorController:
    def __init__(
        self,
        bind_addr: str = LOCALHOST,
        bind_port: int = TURBINE_ACTUATOR_PORT,
    ):
        self._bind    = (bind_addr, bind_port)
        self._state   = ActuatorState()
        self._running = False
        self._seq     = 0

        # Raw TCP server socket
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(self._bind)
        self._server.listen(5)
        logger.info("[ACTUATOR] Listening on %s:%d (TCP)", *self._bind)

        # Rate-limiting: track command timestamps per client
        self._rate_table: dict[str, list[float]] = {}
        self._rate_lock  = threading.Lock()

    def start(self):
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True, name="act-accept").start()
        threading.Thread(target=self._physics_loop, daemon=True, name="act-physics").start()
        logger.info("[ACTUATOR] Started")

    def stop(self):
        self._running = False
        self._server.close()
        logger.info("[ACTUATOR] Stopped")

    # ── Accept loop ───────────────────────────────────────────────────────────

    def _accept_loop(self):
        self._server.settimeout(1.0)
        while self._running:
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            logger.info("[ACTUATOR] Connection from %s", addr)
            threading.Thread(
                target=self._client_handler,
                args=(conn, addr),
                daemon=True,
            ).start()

    # ── Per-client handler ────────────────────────────────────────────────────

    def _client_handler(self, conn: socket.socket, addr):
        buf = b""
        conn.settimeout(5.0)
        try:
            while self._running:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    # Send a status heartbeat
                    self._send_status(conn, addr)
                    continue
                if not chunk:
                    break
                buf += chunk
                # Process all complete frames in buffer
                buf = self._process_buffer(buf, conn, addr)
        except OSError as exc:
            logger.warning("[ACTUATOR] Client %s error: %s", addr, exc)
        finally:
            conn.close()
            logger.info("[ACTUATOR] Client %s disconnected", addr)

    def _process_buffer(self, buf: bytes, conn: socket.socket, addr) -> bytes:
        """Parse as many complete TWCP frames as possible from buf."""
        from protocol.frame import _HEADER_BYTES, _CRC_BYTES
        import struct as _struct

        while len(buf) >= _HEADER_BYTES + _CRC_BYTES:
            # Peek at length field (offset 9, 2 bytes)
            plen = _struct.unpack_from("!H", buf, 9)[0]
            frame_end = _HEADER_BYTES + plen + _CRC_BYTES
            if len(buf) < frame_end:
                break   # Incomplete frame — wait for more data
            raw    = buf[:frame_end]
            buf    = buf[frame_end:]
            parsed = parse_frame(raw)
            if parsed["valid"]:
                self._dispatch(parsed, conn, addr)
            else:
                logger.warning("[ACTUATOR] Invalid frame: %s", parsed["error"])
        return buf

    def _dispatch(self, frame: dict, conn: socket.socket, addr):
        client_key = str(addr)
        if not self._rate_ok(client_key):
            logger.warning("[ACTUATOR] Rate limit exceeded for %s", addr)
            self._tx(conn, build_frame(MSG_NACK, frame["seq"], b"rate_limit"))
            return

        mtype = frame["type"]
        time.sleep(ACTUATOR_DELAY_S)   # simulate actuator response delay

        if mtype == MSG_YAW_CMD:
            try:
                cmd = json.loads(frame["payload"])
                result = self._state.apply_yaw(float(cmd["angle_deg"]))
            except Exception as exc:
                result = {"ok": False, "reason": str(exc)}
            self._ack_or_nack(frame, conn, result)
            logger.info("[ACTUATOR] YAW_CMD → %s", result)

        elif mtype == MSG_PITCH_CMD:
            try:
                cmd = json.loads(frame["payload"])
                result = self._state.apply_pitch(float(cmd["angle_deg"]))
            except Exception as exc:
                result = {"ok": False, "reason": str(exc)}
            self._ack_or_nack(frame, conn, result)
            logger.info("[ACTUATOR] PITCH_CMD → %s", result)

        elif mtype == MSG_EMERGENCY_STOP:
            result = self._state.emergency_stop()
            self._ack_or_nack(frame, conn, result)

        elif mtype == MSG_ACK:
            logger.debug("[ACTUATOR] ACK seq=%d", frame["seq"])

        else:
            logger.debug("[ACTUATOR] Unhandled type 0x%02X", mtype)

    def _ack_or_nack(self, frame: dict, conn: socket.socket, result: dict):
        if result.get("ok"):
            resp = build_frame(MSG_ACK, frame["seq"], json.dumps(result).encode())
        else:
            resp = build_frame(MSG_NACK, frame["seq"], json.dumps(result).encode())
        self._tx(conn, resp)
        self._send_status(conn, None)

    def _send_status(self, conn: socket.socket, addr):
        status = self._state.snapshot()
        frame  = build_frame(MSG_STATUS, self._seq, json.dumps(status).encode())
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        self._tx(conn, frame)

    # ── Physics tick ──────────────────────────────────────────────────────────

    def _physics_loop(self):
        while self._running:
            self._state.tick(0.1)
            time.sleep(0.1)

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def _rate_ok(self, client_key: str) -> bool:
        now = time.monotonic()
        with self._rate_lock:
            ts_list = self._rate_table.setdefault(client_key, [])
            ts_list = [t for t in ts_list if now - t < 1.0]
            self._rate_table[client_key] = ts_list
            if len(ts_list) >= RATE_LIMIT_CMDS_PER_S:
                return False
            ts_list.append(now)
            return True

    @staticmethod
    def _tx(conn: socket.socket, frame: bytes):
        try:
            conn.sendall(frame)
        except OSError as exc:
            logger.error("[ACTUATOR] TX error: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="TWCP Turbine Actuator Controller")
    parser.add_argument("--bind", default=LOCALHOST)
    parser.add_argument("--port", type=int, default=TURBINE_ACTUATOR_PORT)
    args = parser.parse_args()

    ctrl = ActuatorController(bind_addr=args.bind, bind_port=args.port)
    ctrl.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ctrl.stop()
        logger.info("[ACTUATOR] Exiting")


if __name__ == "__main__":
    main()
