"""
tests/test_frame.py — Unit tests for the TWCP custom frame format.
"""

import struct
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.frame import build_frame, parse_frame, frame_size, _HEADER_BYTES, _CRC_BYTES
from protocol.message_types import (
    MSG_SENSOR_DATA,
    MSG_ACK,
    MSG_YAW_CMD,
    FLAG_RELIABLE,
)
from config import TWCP_SYNC_WORD, TWCP_VERSION


class TestFrameBuild(unittest.TestCase):

    def test_empty_payload(self):
        frame = build_frame(MSG_ACK, 0, b"")
        self.assertGreaterEqual(len(frame), _HEADER_BYTES + _CRC_BYTES)

    def test_sync_word_present(self):
        frame = build_frame(MSG_SENSOR_DATA, 1, b"hello")
        self.assertTrue(frame.startswith(TWCP_SYNC_WORD))

    def test_version_field(self):
        frame = build_frame(MSG_SENSOR_DATA, 1, b"test")
        _, ver, *_ = struct.unpack_from("!2sBBBIH", frame, 0)
        self.assertEqual(ver, TWCP_VERSION)

    def test_type_field(self):
        frame = build_frame(MSG_YAW_CMD, 5, b"payload")
        _, _, mtype, *_ = struct.unpack_from("!2sBBBIH", frame, 0)
        self.assertEqual(mtype, MSG_YAW_CMD)

    def test_seq_field(self):
        frame = build_frame(MSG_ACK, 42, b"x")
        _, _, _, _, seq, _ = struct.unpack_from("!2sBBBIH", frame, 0)
        self.assertEqual(seq, 42)

    def test_payload_length_field(self):
        payload = b"abcdefgh"
        frame   = build_frame(MSG_ACK, 0, payload)
        _, _, _, _, _, plen = struct.unpack_from("!2sBBBIH", frame, 0)
        self.assertEqual(plen, len(payload))

    def test_total_length(self):
        payload = b"X" * 100
        frame   = build_frame(MSG_SENSOR_DATA, 0, payload)
        self.assertEqual(len(frame), frame_size(len(payload)))

    def test_flags_field(self):
        frame = build_frame(MSG_YAW_CMD, 0, b"cmd", flags=FLAG_RELIABLE)
        _, _, _, flags, *_ = struct.unpack_from("!2sBBBIH", frame, 0)
        self.assertEqual(flags, FLAG_RELIABLE)

    def test_payload_too_large_raises(self):
        with self.assertRaises(ValueError):
            build_frame(MSG_SENSOR_DATA, 0, b"X" * 70000)


class TestFrameParse(unittest.TestCase):

    def _roundtrip(self, msg_type, seq, payload, flags=0):
        frame  = build_frame(msg_type, seq, payload, flags=flags)
        parsed = parse_frame(frame)
        return parsed

    def test_valid_roundtrip(self):
        parsed = self._roundtrip(MSG_ACK, 7, b"hello")
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["type"],    MSG_ACK)
        self.assertEqual(parsed["seq"],     7)
        self.assertEqual(parsed["payload"], b"hello")

    def test_valid_empty_payload(self):
        from protocol.message_types import MSG_HEARTBEAT
        parsed = self._roundtrip(MSG_HEARTBEAT, 0, b"")
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["payload"], b"")

    def test_crc_corruption_detected(self):
        frame = bytearray(build_frame(MSG_SENSOR_DATA, 1, b"data"))
        frame[-1] ^= 0xFF   # Flip last byte of CRC
        parsed = parse_frame(bytes(frame))
        self.assertFalse(parsed["valid"])
        self.assertIn("CRC", parsed["error"])

    def test_bad_sync_detected(self):
        frame = bytearray(build_frame(MSG_SENSOR_DATA, 1, b"data"))
        frame[0] = 0x00   # Corrupt sync word
        parsed = parse_frame(bytes(frame))
        self.assertFalse(parsed["valid"])
        self.assertIn("sync", parsed["error"].lower())

    def test_truncated_frame_detected(self):
        frame  = build_frame(MSG_SENSOR_DATA, 1, b"data")
        parsed = parse_frame(frame[:5])   # Too short
        self.assertFalse(parsed["valid"])

    def test_payload_corruption_detected(self):
        frame = bytearray(build_frame(MSG_SENSOR_DATA, 1, b"original"))
        # Corrupt one payload byte
        frame[_HEADER_BYTES] ^= 0x55
        parsed = parse_frame(bytes(frame))
        self.assertFalse(parsed["valid"])

    def test_flags_preserved(self):
        parsed = self._roundtrip(MSG_YAW_CMD, 0, b"cmd", flags=FLAG_RELIABLE)
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["flags"], FLAG_RELIABLE)

    def test_large_seq_number(self):
        seq    = 0xFFFFFFFF
        parsed = self._roundtrip(MSG_ACK, seq, b"big_seq")
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["seq"], seq)


class TestFrameSize(unittest.TestCase):

    def test_zero_payload(self):
        self.assertEqual(frame_size(0), _HEADER_BYTES + _CRC_BYTES)

    def test_nonzero_payload(self):
        self.assertEqual(frame_size(100), _HEADER_BYTES + 100 + _CRC_BYTES)


if __name__ == "__main__":
    unittest.main()
