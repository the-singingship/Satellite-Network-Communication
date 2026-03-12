"""
ground/control_station.py — Process 4: Ground Control Station.

Transport: TCP, Port 5004  (receives sensor data + sends commands)
Also listens on UDP Port 5003 (channel stats from relay).

Features:
  - Receives and displays sensor data from turbines
  - Sends yaw/pitch control commands via relay
  - Interactive CLI for operator input
  - Displays channel status and link quality metrics
"""

import json
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
    GROUND_CONTROL_PORT,
    LEO_RELAY_PORT,
    TURBINE_ACTUATOR_PORT,
    LOCALHOST,
)
from protocol.frame import build_frame, parse_frame
from protocol.message_types import (
    MSG_SENSOR_DATA,
    MSG_SENSOR_BATCH,
    MSG_YAW_CMD,
    MSG_PITCH_CMD,
    MSG_EMERGENCY_STOP,
    MSG_ACK,
    MSG_NACK,
    MSG_HEARTBEAT,
    MSG_STATUS,
    FLAG_RELIABLE,
)
from utils.logger import setup_logging

logger = setup_logging("control_station")

_HELP = """
TWCP Ground Control Station — Commands:
  yaw   <angle_deg>   Set turbine yaw angle (0–360)
  pitch <angle_deg>   Set turbine pitch angle (0–90)
  estop               Trigger emergency stop
  status              Show latest sensor reading
  stats               Show link statistics
  help                Show this help
  quit / exit         Shutdown
"""


class GroundControlStation:
    def __init__(
        self,
        bind_addr:    str = LOCALHOST,
        bind_port:    int = GROUND_CONTROL_PORT,
        relay_addr:   str = LOCALHOST,
        relay_port:   int = LEO_RELAY_PORT,
        actuator_addr: str = LOCALHOST,
        actuator_port: int = TURBINE_ACTUATOR_PORT,
    ):
        self._bind         = (bind_addr,     bind_port)
        self._relay        = (relay_addr,    relay_port)
        self._actuator     = (actuator_addr, actuator_port)
        self._running      = False
        self._seq          = 0
        self._latest       : dict | None = None
        self._link_stats   : dict = {}
        self._lock         = threading.Lock()

        # UDP socket for receiving sensor data (from relay)
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_sock.bind(self._bind)
        logger.info("[GROUND] UDP listening on %s:%d", *self._bind)

        # TCP connection to actuator controller
        self._tcp_conn: socket.socket | None = None
        self._tcp_lock = threading.Lock()

    def start(self):
        self._running = True
        threading.Thread(target=self._udp_recv_loop, daemon=True, name="gcs-udp").start()
        threading.Thread(target=self._tcp_connect,   daemon=True, name="gcs-tcp-connect").start()
        logger.info("[GROUND] Started")

    def stop(self):
        self._running = False
        self._udp_sock.close()
        with self._tcp_lock:
            if self._tcp_conn:
                self._tcp_conn.close()
        logger.info("[GROUND] Stopped")

    # ── UDP receive loop (sensor data) ────────────────────────────────────────

    def _udp_recv_loop(self):
        self._udp_sock.settimeout(1.0)
        while self._running:
            try:
                raw, addr = self._udp_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            parsed = parse_frame(raw)
            if not parsed["valid"]:
                logger.debug("[GROUND] Bad frame from %s", addr)
                continue

            mtype = parsed["type"]
            if mtype == MSG_SENSOR_DATA:
                self._handle_sensor(parsed)
            elif mtype == MSG_SENSOR_BATCH:
                self._handle_batch(parsed)
            elif mtype == MSG_HEARTBEAT:
                logger.debug("[GROUND] Heartbeat from %s", addr)
            elif mtype == MSG_STATUS:
                self._handle_status(parsed)
            elif mtype == MSG_ACK:
                logger.debug("[GROUND] ACK seq=%d", parsed["seq"])
            else:
                logger.debug("[GROUND] UDP type 0x%02X from %s", mtype, addr)

    def _handle_sensor(self, frame: dict):
        try:
            data = json.loads(frame["payload"])
        except json.JSONDecodeError:
            return
        with self._lock:
            self._latest = data
        logger.info(
            "[GROUND] SENSOR  wind=%.1f m/s  yaw=%.1f°  pitch=%.1f°  power=%.0f kW  temp=%.1f°C",
            data.get("wind_speed_ms", 0),
            data.get("yaw_angle_deg", 0),
            data.get("pitch_angle_deg", 0),
            data.get("power_kw", 0),
            data.get("nacelle_temp_c", 0),
        )

    def _handle_batch(self, frame: dict):
        try:
            batch = json.loads(frame["payload"])
        except json.JSONDecodeError:
            return
        logger.info("[GROUND] BATCH of %d readings received", len(batch))
        if batch:
            with self._lock:
                self._latest = batch[-1]

    def _handle_status(self, frame: dict):
        try:
            data = json.loads(frame["payload"])
        except json.JSONDecodeError:
            return
        logger.info(
            "[GROUND] STATUS  yaw_actual=%.1f°  pitch_actual=%.1f°  e_stop=%s",
            data.get("yaw_actual", 0),
            data.get("pitch_actual", 0),
            data.get("e_stop", False),
        )

    # ── TCP connection to actuator ────────────────────────────────────────────

    def _tcp_connect(self):
        """Continuously try to connect to the actuator controller."""
        while self._running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect(self._actuator)
                sock.settimeout(None)
                with self._tcp_lock:
                    self._tcp_conn = sock
                logger.info("[GROUND] TCP connected to actuator %s:%d", *self._actuator)
                self._tcp_recv_loop(sock)
            except (socket.timeout, ConnectionRefusedError, OSError) as exc:
                logger.warning("[GROUND] Actuator TCP connect failed: %s — retry in 3s", exc)
                time.sleep(3)
            finally:
                with self._tcp_lock:
                    self._tcp_conn = None

    def _tcp_recv_loop(self, sock: socket.socket):
        buf = b""
        sock.settimeout(2.0)
        while self._running:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            buf = self._process_tcp_buf(buf)

    def _process_tcp_buf(self, buf: bytes) -> bytes:
        from protocol.frame import _HEADER_BYTES, _CRC_BYTES
        while len(buf) >= _HEADER_BYTES + _CRC_BYTES:
            plen = struct.unpack_from("!H", buf, 9)[0]
            frame_end = _HEADER_BYTES + plen + _CRC_BYTES
            if len(buf) < frame_end:
                break
            raw    = buf[:frame_end]
            buf    = buf[frame_end:]
            parsed = parse_frame(raw)
            if parsed["valid"]:
                mtype = parsed["type"]
                if mtype == MSG_ACK:
                    logger.info("[GROUND] Command ACK'd seq=%d", parsed["seq"])
                elif mtype == MSG_NACK:
                    try:
                        reason = json.loads(parsed["payload"])
                    except Exception:
                        reason = parsed["payload"].decode(errors="replace")
                    logger.warning("[GROUND] Command NACK'd: %s", reason)
                elif mtype == MSG_STATUS:
                    self._handle_status(parsed)
        return buf

    # ── Command sending ───────────────────────────────────────────────────────

    def send_yaw(self, angle_deg: float) -> bool:
        payload = json.dumps({"angle_deg": angle_deg, "turbine_id": "TURBINE-001"}).encode()
        return self._send_tcp(MSG_YAW_CMD, payload)

    def send_pitch(self, angle_deg: float) -> bool:
        payload = json.dumps({"angle_deg": angle_deg, "turbine_id": "TURBINE-001"}).encode()
        return self._send_tcp(MSG_PITCH_CMD, payload)

    def send_emergency_stop(self) -> bool:
        return self._send_tcp(MSG_EMERGENCY_STOP, b"{}")

    def _send_tcp(self, msg_type: int, payload: bytes) -> bool:
        frame = build_frame(msg_type, self._seq, payload, flags=FLAG_RELIABLE)
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        with self._tcp_lock:
            if not self._tcp_conn:
                logger.warning("[GROUND] No TCP connection to actuator")
                return False
            try:
                self._tcp_conn.sendall(frame)
                return True
            except OSError as exc:
                logger.error("[GROUND] TCP send error: %s", exc)
                return False

    # ── CLI ───────────────────────────────────────────────────────────────────

    def run_cli(self):
        print(_HELP)
        while self._running:
            try:
                line = input("gcs> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not line:
                continue
            parts = line.split()
            cmd   = parts[0].lower()

            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(_HELP)
            elif cmd == "status":
                with self._lock:
                    data = self._latest
                if data:
                    print(json.dumps(data, indent=2))
                else:
                    print("No sensor data received yet.")
            elif cmd == "stats":
                stats = self._link_stats
                if stats:
                    print(json.dumps(stats, indent=2))
                else:
                    print("No link statistics available yet.")
            elif cmd == "yaw" and len(parts) == 2:
                try:
                    angle = float(parts[1])
                    ok    = self.send_yaw(angle)
                    print(f"Yaw command {'sent' if ok else 'FAILED'}")
                except ValueError:
                    print("Usage: yaw <angle_deg>")
            elif cmd == "pitch" and len(parts) == 2:
                try:
                    angle = float(parts[1])
                    ok    = self.send_pitch(angle)
                    print(f"Pitch command {'sent' if ok else 'FAILED'}")
                except ValueError:
                    print("Usage: pitch <angle_deg>")
            elif cmd == "estop":
                ok = self.send_emergency_stop()
                print(f"Emergency stop {'sent' if ok else 'FAILED'}")
            else:
                print(f"Unknown command: {line!r}  (type 'help')")

        self.stop()


def main():
    parser = argparse.ArgumentParser(description="TWCP Ground Control Station")
    parser.add_argument("--bind",           default=LOCALHOST)
    parser.add_argument("--port",           type=int, default=GROUND_CONTROL_PORT)
    parser.add_argument("--relay",          default=LOCALHOST)
    parser.add_argument("--relay-port",     type=int, default=LEO_RELAY_PORT)
    parser.add_argument("--actuator",       default=LOCALHOST)
    parser.add_argument("--actuator-port",  type=int, default=TURBINE_ACTUATOR_PORT)
    parser.add_argument("--no-cli",         action="store_true", help="Skip interactive CLI")
    args = parser.parse_args()

    gcs = GroundControlStation(
        bind_addr     = args.bind,
        bind_port     = args.port,
        relay_addr    = args.relay,
        relay_port    = args.relay_port,
        actuator_addr = args.actuator,
        actuator_port = args.actuator_port,
    )
    gcs.start()

    if args.no_cli:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            gcs.stop()
    else:
        gcs.run_cli()

    logger.info("[GROUND] Exiting")


if __name__ == "__main__":
    main()
