"""
turbine/sensor_emulator.py — Process 1: Wind Turbine Sensor Emulator.

Transport: UDP raw socket, Port 5001
Sends periodic sensor readings to the LEO relay (Port 5003).
Responds to control commands forwarded by the relay.

Sensor channels:
  wind_speed_ms, yaw_angle_deg, pitch_angle_deg, rotor_rpm,
  power_kw, nacelle_temp_c, vibration_ms2
"""

import json
import math
import random
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
    TURBINE_SENSOR_PORT,
    LEO_RELAY_PORT,
    LOCALHOST,
    SENSOR_INTERVAL_S,
    TURBINE_ID,
)
from protocol.frame import build_frame, parse_frame
from protocol.message_types import (
    MSG_SENSOR_DATA,
    MSG_SENSOR_BATCH,
    MSG_YAW_CMD,
    MSG_PITCH_CMD,
    MSG_EMERGENCY_STOP,
    MSG_ACK,
    MSG_HEARTBEAT,
    MSG_STATUS,
    FLAG_RELIABLE,
)
from utils.logger import setup_logging

logger = setup_logging("sensor_emulator")


# ── Turbine physics model ─────────────────────────────────────────────────────

class TurbineModel:
    """
    Simple physics model of a 5-MW offshore wind turbine.

    Tracks current state and evolves it on each tick so that the emitted
    readings follow a physically plausible trajectory.
    """

    # Rated (nameplate) values
    RATED_WIND_MS   = 12.0
    RATED_POWER_KW  = 5000.0
    MIN_WIND_CUTOUT = 3.0    # cut-in speed
    MAX_WIND_CUTOUT = 25.0   # storm shut-down

    def __init__(self):
        self.wind_speed_ms    = random.uniform(6, 14)
        self.yaw_angle_deg    = random.uniform(0, 360)
        self.pitch_angle_deg  = random.uniform(2, 15)
        self.rotor_rpm        = 10.0
        self.power_kw         = 0.0
        self.nacelle_temp_c   = 25.0
        self.vibration_ms2    = 0.02
        self._target_yaw      = self.yaw_angle_deg
        self._target_pitch    = self.pitch_angle_deg
        self._e_stop          = False
        self._lock            = threading.Lock()

    # Turbine physics tuning constants
    WIND_DRIFT_STDDEV      = 0.3    # m/s standard deviation of wind random walk
    MEAN_REVERSION_FACTOR  = 0.05   # rate of mean-reversion toward target wind
    TARGET_WIND_SPEED_MS   = 10.0   # long-term mean wind speed

    def tick(self, dt: float = SENSOR_INTERVAL_S):
        """Evolve the model by *dt* seconds."""
        with self._lock:
            # Wind: random walk with mean-reversion
            self.wind_speed_ms += (
                random.gauss(0, self.WIND_DRIFT_STDDEV)
                + self.MEAN_REVERSION_FACTOR * (self.TARGET_WIND_SPEED_MS - self.wind_speed_ms)
            )
            self.wind_speed_ms  = max(0.0, min(30.0, self.wind_speed_ms))

            if self._e_stop:
                self.pitch_angle_deg = min(90.0, self.pitch_angle_deg + 5.0)
                self.rotor_rpm       = max(0.0, self.rotor_rpm - 2.0)
                self.power_kw        = 0.0
            else:
                # Yaw tracking
                yaw_err = (self._target_yaw - self.yaw_angle_deg + 180) % 360 - 180
                self.yaw_angle_deg = (self.yaw_angle_deg + 0.5 * yaw_err) % 360

                # Pitch tracking
                pitch_err             = self._target_pitch - self.pitch_angle_deg
                self.pitch_angle_deg += 0.2 * pitch_err
                self.pitch_angle_deg  = max(0.0, min(90.0, self.pitch_angle_deg))

                # RPM
                if self.MIN_WIND_CUTOUT <= self.wind_speed_ms <= self.MAX_WIND_CUTOUT:
                    target_rpm = 6.0 + 5.0 * (self.wind_speed_ms - 3) / 22.0
                else:
                    target_rpm = 0.0
                self.rotor_rpm += 0.1 * (target_rpm - self.rotor_rpm)
                self.rotor_rpm  = max(0.0, self.rotor_rpm)

                # Power: Cp curve approximation (cubic below rated, flat above)
                if self.wind_speed_ms < self.MIN_WIND_CUTOUT or self.wind_speed_ms > self.MAX_WIND_CUTOUT:
                    self.power_kw = 0.0
                elif self.wind_speed_ms <= self.RATED_WIND_MS:
                    frac          = (self.wind_speed_ms - self.MIN_WIND_CUTOUT) / (self.RATED_WIND_MS - self.MIN_WIND_CUTOUT)
                    self.power_kw = self.RATED_POWER_KW * (frac ** 3)
                else:
                    self.power_kw = self.RATED_POWER_KW

            # Temperature: rises with high power output, cooled by wind
            temp_target           = 20 + 0.005 * self.power_kw - 0.3 * self.wind_speed_ms
            self.nacelle_temp_c  += 0.05 * (temp_target - self.nacelle_temp_c) + random.gauss(0, 0.1)
            self.nacelle_temp_c   = max(-10, min(80, self.nacelle_temp_c))

            # Vibration: higher at high RPM, random spikes
            self.vibration_ms2 = 0.01 * self.rotor_rpm + random.uniform(0, 0.05)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "turbine_id":      TURBINE_ID,
                "timestamp":       time.time(),
                "wind_speed_ms":   round(self.wind_speed_ms,   2),
                "yaw_angle_deg":   round(self.yaw_angle_deg,   2),
                "pitch_angle_deg": round(self.pitch_angle_deg, 2),
                "rotor_rpm":       round(self.rotor_rpm,       2),
                "power_kw":        round(self.power_kw,        1),
                "nacelle_temp_c":  round(self.nacelle_temp_c,  1),
                "vibration_ms2":   round(self.vibration_ms2,   4),
                "e_stop":          self._e_stop,
            }

    def set_yaw(self, angle_deg: float):
        with self._lock:
            self._target_yaw = angle_deg % 360

    def set_pitch(self, angle_deg: float):
        with self._lock:
            self._target_pitch = max(0.0, min(90.0, angle_deg))

    def emergency_stop(self):
        with self._lock:
            self._e_stop = True
            logger.warning("[TURBINE] EMERGENCY STOP engaged!")


# ── Sensor Emulator process ───────────────────────────────────────────────────

class SensorEmulator:
    def __init__(
        self,
        bind_addr: str = LOCALHOST,
        bind_port: int = TURBINE_SENSOR_PORT,
        relay_addr: str = LOCALHOST,
        relay_port: int = LEO_RELAY_PORT,
        interval: float = SENSOR_INTERVAL_S,
    ):
        self._bind    = (bind_addr, bind_port)
        self._relay   = (relay_addr, relay_port)
        self._interval = interval
        self._model   = TurbineModel()
        self._seq     = 0
        self._running = False

        # Raw UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(self._bind)
        logger.info("[SENSOR] Bound to %s:%d → relay %s:%d", *self._bind, *self._relay)

    def start(self):
        self._running = True
        threading.Thread(target=self._recv_loop,  daemon=True, name="sensor-rx").start()
        threading.Thread(target=self._send_loop,  daemon=True, name="sensor-tx").start()
        threading.Thread(target=self._heartbeat,  daemon=True, name="sensor-hb").start()
        logger.info("[SENSOR] Started")

    def stop(self):
        self._running = False
        self._sock.close()
        logger.info("[SENSOR] Stopped")

    # ── Transmission loop ─────────────────────────────────────────────────────

    def _send_loop(self):
        batch: list[dict] = []
        batch_size = 5   # bundle 5 readings into a SENSOR_BATCH

        while self._running:
            self._model.tick(self._interval)
            reading = self._model.snapshot()
            batch.append(reading)

            # Always send individual reading
            payload = json.dumps(reading).encode()
            frame   = build_frame(MSG_SENSOR_DATA, self._seq, payload)
            self._tx(frame)
            logger.info(
                "[SENSOR] Sent SENSOR_DATA seq=%d  wind=%.1f m/s  power=%.0f kW",
                self._seq, reading["wind_speed_ms"], reading["power_kw"],
            )
            self._seq = (self._seq + 1) & 0xFFFFFFFF

            # Every `batch_size` readings send a batch too
            if len(batch) >= batch_size:
                batch_payload = json.dumps(batch).encode()
                frame_b       = build_frame(MSG_SENSOR_BATCH, self._seq, batch_payload)
                self._tx(frame_b)
                logger.debug("[SENSOR] Sent SENSOR_BATCH of %d readings", batch_size)
                self._seq = (self._seq + 1) & 0xFFFFFFFF
                batch.clear()

            time.sleep(self._interval)

    # ── Receive loop (handles commands from relay) ────────────────────────────

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
                logger.warning("[SENSOR] Bad frame from %s: %s", addr, parsed["error"])
                continue

            mtype = parsed["type"]
            if mtype == MSG_YAW_CMD:
                self._handle_yaw(parsed, addr)
            elif mtype == MSG_PITCH_CMD:
                self._handle_pitch(parsed, addr)
            elif mtype == MSG_EMERGENCY_STOP:
                self._model.emergency_stop()
                self._send_ack(parsed["seq"], addr)
            elif mtype == MSG_ACK:
                logger.debug("[SENSOR] ACK received seq=%d", parsed["seq"])
            else:
                logger.debug("[SENSOR] Unhandled type 0x%02X from %s", mtype, addr)

    def _handle_yaw(self, frame: dict, addr):
        try:
            cmd = json.loads(frame["payload"])
            angle = float(cmd["angle_deg"])
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("[SENSOR] Malformed YAW_CMD")
            return
        self._model.set_yaw(angle)
        logger.info("[SENSOR] Yaw set to %.1f°", angle)
        self._send_ack(frame["seq"], addr)

    def _handle_pitch(self, frame: dict, addr):
        try:
            cmd = json.loads(frame["payload"])
            angle = float(cmd["angle_deg"])
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("[SENSOR] Malformed PITCH_CMD")
            return
        self._model.set_pitch(angle)
        logger.info("[SENSOR] Pitch set to %.1f°", angle)
        self._send_ack(frame["seq"], addr)

    def _send_ack(self, seq: int, addr):
        ack = build_frame(MSG_ACK, seq, struct.pack("!I", seq))
        self._tx(ack, addr)

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    def _heartbeat(self):
        while self._running:
            time.sleep(10)
            payload = json.dumps({"turbine_id": TURBINE_ID, "ts": time.time()}).encode()
            frame   = build_frame(MSG_HEARTBEAT, self._seq, payload)
            self._tx(frame)
            self._seq = (self._seq + 1) & 0xFFFFFFFF

    # ── Socket helpers ────────────────────────────────────────────────────────

    def _tx(self, frame: bytes, dest: tuple | None = None):
        try:
            self._sock.sendto(frame, dest or self._relay)
        except OSError as exc:
            logger.error("[SENSOR] TX error: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="TWCP Turbine Sensor Emulator")
    parser.add_argument("--bind",       default=LOCALHOST,          help="Bind address")
    parser.add_argument("--port",       type=int, default=TURBINE_SENSOR_PORT)
    parser.add_argument("--relay",      default=LOCALHOST)
    parser.add_argument("--relay-port", type=int, default=LEO_RELAY_PORT)
    parser.add_argument("--interval",   type=float, default=SENSOR_INTERVAL_S)
    args = parser.parse_args()

    em = SensorEmulator(
        bind_addr  = args.bind,
        bind_port  = args.port,
        relay_addr = args.relay,
        relay_port = args.relay_port,
        interval   = args.interval,
    )
    em.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        em.stop()
        logger.info("[SENSOR] Exiting")


if __name__ == "__main__":
    main()
