"""
tests/test_integration.py — Integration tests: multi-process protocol flows.

These tests spin up lightweight in-process versions of the components
(no real subprocesses) and verify end-to-end message flows.
"""

import json
import socket
import struct
import threading
import time
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.frame import build_frame, parse_frame
from protocol.message_types import (
    MSG_SENSOR_DATA,
    MSG_YAW_CMD,
    MSG_ACK,
    MSG_DISCOVER,
    MSG_NEGOTIATE,
    MSG_AGREE,
    FLAG_RELIABLE,
)
from protocol.handshake import HandshakeInitiator, HandshakeResponder
from discovery.negotiation import negotiate_capabilities
from bonus.security import SecurityGate, ChallengeAuth, ReplayDetector, RateLimiter


# ── Helper: loopback UDP pair ─────────────────────────────────────────────────

def _udp_pair(port_a: int, port_b: int):
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.bind(("127.0.0.1", port_a))
    b.bind(("127.0.0.1", port_b))
    a.settimeout(3.0)
    b.settimeout(3.0)
    return a, b


# ── Integration tests ─────────────────────────────────────────────────────────

class TestSensorDataFlow(unittest.TestCase):
    """Sensor data frame travels from turbine-side socket to ground-side socket."""

    BASE_PORT = 19300

    def setUp(self):
        self.sock_tx, self.sock_rx = _udp_pair(self.BASE_PORT, self.BASE_PORT + 1)
        self.dest = ("127.0.0.1", self.BASE_PORT + 1)

    def tearDown(self):
        self.sock_tx.close()
        self.sock_rx.close()

    def test_sensor_data_roundtrip(self):
        reading = {
            "turbine_id":      "TURBINE-TEST",
            "wind_speed_ms":   10.5,
            "yaw_angle_deg":   180.0,
            "pitch_angle_deg": 12.0,
            "rotor_rpm":       11.5,
            "power_kw":        3200.0,
            "nacelle_temp_c":  28.3,
        }
        payload = json.dumps(reading).encode()
        frame   = build_frame(MSG_SENSOR_DATA, 1, payload)
        self.sock_tx.sendto(frame, self.dest)

        raw, _ = self.sock_rx.recvfrom(65535)
        parsed = parse_frame(raw)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["type"], MSG_SENSOR_DATA)
        data_back = json.loads(parsed["payload"])
        self.assertAlmostEqual(data_back["wind_speed_ms"], 10.5)


class TestYawCommandFlow(unittest.TestCase):
    """YAW_CMD sent from ground side is received and ACK'd by turbine side."""

    BASE_PORT = 19310

    def setUp(self):
        self.gcs, self.turbine = _udp_pair(self.BASE_PORT, self.BASE_PORT + 1)
        self.turbine_addr = ("127.0.0.1", self.BASE_PORT + 1)
        self.gcs_addr     = ("127.0.0.1", self.BASE_PORT)

    def tearDown(self):
        self.gcs.close()
        self.turbine.close()

    def test_yaw_cmd_and_ack(self):
        cmd = json.dumps({"angle_deg": 270.0}).encode()
        frame = build_frame(MSG_YAW_CMD, 5, cmd, flags=FLAG_RELIABLE)
        self.gcs.sendto(frame, self.turbine_addr)

        # Turbine receives and sends ACK
        raw, addr = self.turbine.recvfrom(4096)
        parsed    = parse_frame(raw)
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["type"], MSG_YAW_CMD)

        ack = build_frame(MSG_ACK, parsed["seq"], struct.pack("!I", parsed["seq"]))
        self.turbine.sendto(ack, addr)

        # GCS receives ACK
        raw_ack, _ = self.gcs.recvfrom(4096)
        parsed_ack = parse_frame(raw_ack)
        self.assertTrue(parsed_ack["valid"])
        self.assertEqual(parsed_ack["type"], MSG_ACK)
        self.assertEqual(parsed_ack["seq"],  5)


class TestHandshakeFlow(unittest.TestCase):
    """Full 4-step DISCOVER → NEGOTIATE → AGREE → ACK handshake."""

    BASE_PORT = 19320

    def setUp(self):
        self.init_sock, self.resp_sock = _udp_pair(self.BASE_PORT, self.BASE_PORT + 1)
        self.resp_addr = ("127.0.0.1", self.BASE_PORT + 1)
        self.init_addr = ("127.0.0.1", self.BASE_PORT)

    def tearDown(self):
        self.init_sock.close()
        self.resp_sock.close()

    def test_full_handshake(self):
        initiator = HandshakeInitiator(
            self.init_sock,
            self.resp_addr,
            node_id   = "TURBINE-001",
            timeout   = 2.0,
            max_retries = 2,
        )
        responder = HandshakeResponder("GROUND-001")

        session_result = {}

        def responder_thread():
            # Step 1: receive DISCOVER
            try:
                raw, addr = self.resp_sock.recvfrom(4096)
            except socket.timeout:
                return
            frame = parse_frame(raw)
            ctx = responder.handle_discover(frame, addr, self.resp_sock)
            if ctx is None:
                return
            # Step 3: receive AGREE
            try:
                raw2, addr2 = self.resp_sock.recvfrom(4096)
            except socket.timeout:
                return
            frame2 = parse_frame(raw2)
            responder.handle_agree(frame2, addr2, self.resp_sock, ctx)
            session_result["responder_ctx"] = ctx

        t = threading.Thread(target=responder_thread, daemon=True)
        t.start()

        session = initiator.connect()
        t.join(timeout=5.0)

        self.assertIsNotNone(session)
        self.assertIn("session_id",  session)
        self.assertIn("peer_id",     session)
        self.assertIn("agreed",      session)
        self.assertGreater(len(session["agreed"]), 0)


class TestNegotiateCapabilities(unittest.TestCase):

    def test_common_caps(self):
        mine   = {"commands": ["YAW_CMD", "PITCH_CMD"], "video": False, "version": "1"}
        theirs = {"commands": ["YAW_CMD", "EMERGENCY_STOP"], "video": False, "version": "1"}
        agreed = negotiate_capabilities(mine, theirs)
        self.assertEqual(agreed["commands"], ["YAW_CMD"])
        self.assertEqual(agreed["version"],  "1")

    def test_no_common_caps(self):
        mine   = {"commands": ["PITCH_CMD"]}
        theirs = {"commands": ["YAW_CMD"]}
        agreed = negotiate_capabilities(mine, theirs)
        self.assertEqual(agreed.get("commands", []), [])

    def test_bool_intersection_true(self):
        mine   = {"video": True}
        theirs = {"video": True}
        agreed = negotiate_capabilities(mine, theirs)
        self.assertTrue(agreed["video"])

    def test_bool_intersection_false(self):
        mine   = {"video": False}
        theirs = {"video": True}
        agreed = negotiate_capabilities(mine, theirs)
        self.assertNotIn("video", agreed)


class TestSecurityGate(unittest.TestCase):

    def test_normal_command_passes(self):
        gate    = SecurityGate()
        frame   = parse_frame(
            build_frame(MSG_YAW_CMD, 1, json.dumps({"angle_deg": 90.0}).encode())
        )
        result = gate.check_frame("127.0.0.1:9999", frame)
        self.assertTrue(result)

    def test_replay_attack_blocked(self):
        gate  = SecurityGate()
        frame = parse_frame(
            build_frame(MSG_YAW_CMD, 1, json.dumps({"angle_deg": 90.0}).encode())
        )
        # First time: OK
        self.assertTrue(gate.check_frame("127.0.0.1:9999", frame))
        # Same seq again: replay!
        self.assertFalse(gate.check_frame("127.0.0.1:9999", frame))

    def test_out_of_range_yaw_blocked(self):
        gate  = SecurityGate()
        frame = parse_frame(
            build_frame(MSG_YAW_CMD, 2, json.dumps({"angle_deg": 999.0}).encode())
        )
        result = gate.check_frame("127.0.0.1:9998", frame)
        self.assertFalse(result)

    def test_rate_limit_exceeded(self):
        gate = SecurityGate()
        src  = "127.0.0.1:7777"
        # Send 6 commands in rapid succession (limit is 5/s)
        for i in range(5):
            frame = parse_frame(
                build_frame(MSG_YAW_CMD, i + 100, json.dumps({"angle_deg": float(i)}).encode())
            )
            gate.check_frame(src, frame)

        frame6 = parse_frame(
            build_frame(MSG_YAW_CMD, 200, json.dumps({"angle_deg": 10.0}).encode())
        )
        result = gate.check_frame(src, frame6)
        self.assertFalse(result)


class TestChallengeAuth(unittest.TestCase):

    def test_correct_response_verifies(self):
        auth      = ChallengeAuth()
        challenge = auth.generate_challenge()
        response  = auth.compute_response(challenge)
        self.assertTrue(auth.verify_response(challenge, response))

    def test_wrong_response_fails(self):
        auth      = ChallengeAuth()
        challenge = auth.generate_challenge()
        bad_resp  = b"\x00" * 32
        self.assertFalse(auth.verify_response(challenge, bad_resp))

    def test_challenge_uniqueness(self):
        auth = ChallengeAuth()
        c1   = auth.generate_challenge()
        c2   = auth.generate_challenge()
        self.assertNotEqual(c1, c2)


if __name__ == "__main__":
    unittest.main()
