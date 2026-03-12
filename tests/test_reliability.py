"""
tests/test_reliability.py — Unit tests for Stop-and-Wait and Selective Repeat ARQ.
"""

import socket
import struct
import threading
import time
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.frame import build_frame, parse_frame
from protocol.reliability import StopAndWaitARQ, SelectiveRepeatARQ
from protocol.message_types import MSG_SENSOR_DATA, MSG_ACK


# ── Loopback UDP helpers ──────────────────────────────────────────────────────

def _udp_pair(port_a: int, port_b: int):
    """Return two bound UDP sockets on localhost."""
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.bind(("127.0.0.1", port_a))
    b.bind(("127.0.0.1", port_b))
    a.settimeout(2.0)
    b.settimeout(2.0)
    return a, b


class TestStopAndWaitARQ(unittest.TestCase):

    def setUp(self):
        self.sa, self.sb = _udp_pair(19101, 19102)
        self.dest_b = ("127.0.0.1", 19102)
        self.dest_a = ("127.0.0.1", 19101)
        self.arq = StopAndWaitARQ(
            self.sa,
            self.dest_b,
            timeout=0.5,
            max_retries=3,
        )

    def tearDown(self):
        self.sa.close()
        self.sb.close()

    def test_send_and_receive_ack(self):
        """Sender gets ACK → returns True."""
        payload = b"test_saw_payload"

        # Receiver thread: echo ACK back
        def receiver():
            try:
                raw, addr = self.sb.recvfrom(4096)
                parsed = parse_frame(raw)
                if parsed["valid"]:
                    ack = StopAndWaitARQ.make_ack(parsed["seq"])
                    self.sb.sendto(ack, addr)
                    # Notify ARQ about ACK
                    self.arq.notify_ack(parsed["seq"], success=True)
            except socket.timeout:
                pass

        t = threading.Thread(target=receiver, daemon=True)
        t.start()

        result = self.arq.send_reliable(MSG_SENSOR_DATA, payload)
        t.join(timeout=2.0)
        self.assertTrue(result)

    def test_send_times_out_without_ack(self):
        """Without an ACK, send_reliable should return False after retries."""
        # Don't start any receiver
        result = self.arq.send_reliable(MSG_SENSOR_DATA, b"no_ack_payload")
        self.assertFalse(result)

    def test_make_ack_is_valid_frame(self):
        ack = StopAndWaitARQ.make_ack(42)
        parsed = parse_frame(ack)
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["type"], MSG_ACK)
        self.assertEqual(parsed["seq"],  42)

    def test_make_nack_is_valid_frame(self):
        nack = StopAndWaitARQ.make_nack(7)
        parsed = parse_frame(nack)
        self.assertTrue(parsed["valid"])
        from protocol.message_types import MSG_NACK
        self.assertEqual(parsed["type"], MSG_NACK)


class TestSelectiveRepeatARQ(unittest.TestCase):

    def setUp(self):
        self.sa, self.sb = _udp_pair(19201, 19202)
        self.dest_b = ("127.0.0.1", 19202)
        self.sr = SelectiveRepeatARQ(
            self.sa,
            self.dest_b,
            window_size=4,
            timeout=0.3,
            max_retries=3,
        )

    def tearDown(self):
        self.sa.close()
        self.sb.close()

    def test_send_returns_seq(self):
        seq = self.sr.send(MSG_SENSOR_DATA, b"hello")
        self.assertIsNotNone(seq)
        self.assertEqual(seq, 0)

    def test_send_fills_window(self):
        seqs = []
        for i in range(4):
            s = self.sr.send(MSG_SENSOR_DATA, f"payload-{i}".encode())
            seqs.append(s)
        self.assertEqual(seqs, [0, 1, 2, 3])

    def test_window_full_returns_none(self):
        """When the window is full, send returns None."""
        for _ in range(4):
            self.sr.send(MSG_SENSOR_DATA, b"fill")
        result = self.sr.send(MSG_SENSOR_DATA, b"overflow")
        self.assertIsNone(result)

    def test_process_ack_slides_window(self):
        self.sr.send(MSG_SENSOR_DATA, b"a")
        self.sr.process_ack(0)
        # Window base should have advanced to 1, allowing another send
        s = self.sr.send(MSG_SENSOR_DATA, b"b")
        self.assertIsNotNone(s)

    def test_receive_in_order(self):
        delivered = self.sr.receive(0, b"first")
        self.assertEqual(delivered, [b"first"])

    def test_receive_out_of_order_then_in_order(self):
        # Receive seq=1 out of order (seq=0 missing)
        d1 = self.sr.receive(1, b"second")
        self.assertEqual(d1, [])   # Can't deliver yet

        # Now receive seq=0 — should deliver both in order
        d2 = self.sr.receive(0, b"first")
        self.assertEqual(d2, [b"first", b"second"])

    def test_make_ack(self):
        ack    = self.sr.make_ack(5)
        parsed = parse_frame(ack)
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["type"], MSG_ACK)
        self.assertEqual(parsed["seq"],  5)


if __name__ == "__main__":
    unittest.main()
