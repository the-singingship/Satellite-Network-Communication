"""
tests/test_channel.py — Unit tests for the LEO channel model.
"""

import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.channel_model import LEOChannel, OrbitalClock


class TestOrbitalClock(unittest.TestCase):

    def setUp(self):
        # time_scale=10000 makes 90-min orbits fly by in milliseconds
        self.clock = OrbitalClock(
            orbital_period_s=90 * 60,
            visibility_s=10 * 60,
            time_scale=10000,
        )

    def test_is_visible_at_start(self):
        # At t=0 we should be inside the first visibility window
        self.assertTrue(self.clock.is_visible())

    def test_phase_is_nonnegative(self):
        self.assertGreaterEqual(self.clock._phase(), 0)

    def test_time_to_next_pass_when_visible(self):
        # When visible, time_to_next_pass should be 0
        if self.clock.is_visible():
            self.assertEqual(self.clock.time_to_next_pass(), 0.0)

    def test_pass_edge_factor_range(self):
        factor = self.clock.pass_edge_factor()
        self.assertGreaterEqual(factor, 0.0)
        self.assertLessEqual(factor, 1.0)


class TestLEOChannelModel(unittest.TestCase):

    def setUp(self):
        # Use a very fast time-scale so tests don't wait for real orbits
        self.channel = LEOChannel(time_scale=10000, apply_bandwidth_delay=False)

    def tearDown(self):
        self.channel.stop()

    def test_transmit_during_visibility(self):
        """During the visibility window, most packets should be delivered."""
        # Ensure we're visible (start is always visible in our clock)
        if not self.channel.orbital_clock.is_visible():
            self.skipTest("Not in visibility window at test time")

        data       = b"test payload"
        delivered  = 0
        attempts   = 20
        for _ in range(attempts):
            result = self.channel.transmit(data, apply_latency=False)
            if result is not None:
                delivered += 1
        # At least 50% delivery expected (baseline loss ~5%)
        self.assertGreater(delivered, attempts // 2)

    def test_transmit_returns_bytes_or_none(self):
        data   = b"hello"
        result = self.channel.transmit(data, apply_latency=False)
        self.assertIn(type(result), (bytes, type(None)))

    def test_one_way_latency_positive(self):
        for _ in range(10):
            lat = self.channel.one_way_latency_s()
            self.assertGreater(lat, 0)

    def test_doppler_shift_range(self):
        shift = self.channel.doppler_shift_hz()
        self.assertIsInstance(shift, float)

    def test_get_stats_keys(self):
        stats = self.channel.get_stats()
        for key in ("transmitted", "lost", "loss_rate", "visible", "weather", "doppler_hz"):
            self.assertIn(key, stats)

    def test_loss_rate_bounded(self):
        stats = self.channel.get_stats()
        self.assertGreaterEqual(stats["loss_rate"], 0.0)
        self.assertLessEqual(stats["loss_rate"],    1.0)

    def test_data_passes_through_unmodified_most_of_the_time(self):
        """Sanity: correct data usually arrives unchanged."""
        data      = b"TWCP test frame"
        unchanged = 0
        trials    = 100
        for _ in range(trials):
            res = self.channel.transmit(data, apply_latency=False)
            if res == data:
                unchanged += 1
        # At least 70% should arrive unchanged (5% loss, ~1% corruption)
        self.assertGreater(unchanged, trials * 0.7)


if __name__ == "__main__":
    unittest.main()
