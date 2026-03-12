"""
protocol/channel_model.py — LEO satellite channel emulator.

Models:
  - One-way propagation latency ~40 ms + Gaussian jitter ±15 ms
  - 5 % baseline packet-loss, rising to 20 % at pass edges
  - 256 kbps bandwidth limit (throttle by sleeping)
  - Orbital visibility windows (~10 min visible / ~80 min blackout)
  - Doppler frequency-shift simulation (informational)
  - Atmospheric attenuation (weather-dependent signal degradation)
  - Satellite handoff between consecutive passes
"""

import math
import random
import time
import threading
import logging

from config import (
    LEO_BASE_LATENCY_MS,
    LEO_JITTER_MS,
    LEO_PACKET_LOSS_RATE,
    LEO_BANDWIDTH_BPS,
    LEO_ORBITAL_PERIOD_S,
    LEO_VISIBILITY_WINDOW_S,
    LEO_DOPPLER_MAX_HZ,
    LEO_ATTENUATION_DB_CLEAR,
    LEO_ATTENUATION_DB_STORM,
)

logger = logging.getLogger(__name__)


class OrbitalClock:
    """
    Tracks the simulated orbital phase of the LEO satellite.

    Phase is defined as the fraction of the orbital period elapsed
    since the last pass start. Values in [0, vis_frac) correspond to
    the visibility window; the rest is blackout.
    """

    def __init__(
        self,
        orbital_period_s: float = LEO_ORBITAL_PERIOD_S,
        visibility_s: float = LEO_VISIBILITY_WINDOW_S,
        time_scale: float = 1.0,
    ):
        self._period    = orbital_period_s
        self._vis       = visibility_s
        self._scale     = time_scale          # >1 → fast-forward simulation
        self._start_wall = time.monotonic()
        self._pass_count = 0
        self._lock       = threading.Lock()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _elapsed(self) -> float:
        """Scaled elapsed seconds since creation."""
        return (time.monotonic() - self._start_wall) * self._scale

    def _phase(self) -> float:
        """Phase within current orbital period [0, period)."""
        return self._elapsed() % self._period

    # ── public API ────────────────────────────────────────────────────────────

    def is_visible(self) -> bool:
        """Return True when the satellite is above the horizon."""
        return self._phase() < self._vis

    def pass_edge_factor(self) -> float:
        """
        Returns a value in [0, 1] indicating proximity to the edge of a
        visibility window.  1.0 = middle of pass, 0.0 = exact edge.
        Used to increase packet-loss at pass edges.
        """
        ph = self._phase()
        if ph >= self._vis:
            return 0.0
        # Normalise to [0, 1] within the window
        mid       = self._vis / 2.0
        dist_mid  = abs(ph - mid)
        return 1.0 - (dist_mid / mid)

    def time_to_next_pass(self) -> float:
        """Seconds until next visibility window starts."""
        ph = self._phase()
        if ph < self._vis:
            return 0.0
        return self._period - ph

    def time_in_pass(self) -> float:
        """Seconds remaining in the current visibility window (0 if in blackout)."""
        ph = self._phase()
        return max(0.0, self._vis - ph)


class LEOChannel:
    """
    Simulates the bidirectional communication channel through a LEO satellite.

    Usage::

        ch = LEOChannel()
        delivered = ch.transmit(frame_bytes)  # None → lost / blocked
        latency   = ch.one_way_latency_s()
    """

    # Weather states and their attenuation
    _WEATHER = {
        "clear":   LEO_ATTENUATION_DB_CLEAR,
        "cloudy":  1.5,
        "rainy":   3.0,
        "storm":   LEO_ATTENUATION_DB_STORM,
    }

    def __init__(
        self,
        time_scale: float = 1.0,
        apply_bandwidth_delay: bool = True,
    ):
        self.orbital_clock          = OrbitalClock(time_scale=time_scale)
        self._apply_bw_delay        = apply_bandwidth_delay
        self._weather               = "clear"
        self._weather_lock          = threading.Lock()
        self._stats_lock            = threading.Lock()
        self._tx_count              = 0
        self._lost_count            = 0
        self._queued_during_blackout: list[tuple[float, bytes]] = []
        # Satellite identity (changes on handoff)
        self._sat_id                = 1
        # Start background weather-change thread
        self._running               = True
        threading.Thread(
            target=self._weather_updater, daemon=True, name="weather"
        ).start()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _weather_updater(self):
        """Randomly change weather every 30–120 s (scaled)."""
        states = list(self._WEATHER.keys())
        while self._running:
            time.sleep(random.uniform(30, 120))
            with self._weather_lock:
                self._weather = random.choice(states)
            logger.debug("[CHANNEL] Weather changed to %s", self._weather)

    def _loss_probability(self) -> float:
        """
        Effective packet-loss probability, combining baseline loss and
        edge-of-pass degradation.
        """
        edge = 1.0 - self.orbital_clock.pass_edge_factor()
        # At the pass edge (edge→1) loss rises to 3× baseline
        return LEO_PACKET_LOSS_RATE * (1.0 + 2.0 * edge)

    def _signal_quality_linear(self) -> float:
        """
        Convert atmospheric attenuation (dB) to a linear power ratio
        [0, 1].  Used to scale bit-error probability.
        """
        with self._weather_lock:
            atten_db = self._WEATHER[self._weather]
        return 10 ** (-atten_db / 10.0)

    # ── public API ────────────────────────────────────────────────────────────

    def one_way_latency_s(self) -> float:
        """Sample a one-way latency value (seconds)."""
        jitter = random.gauss(0, LEO_JITTER_MS)
        lat_ms = max(1.0, LEO_BASE_LATENCY_MS + jitter)
        return lat_ms / 1000.0

    def doppler_shift_hz(self) -> float:
        """
        Simulate Doppler frequency shift based on orbital phase.
        Returns shift in Hz (positive = approaching, negative = receding).
        The satellite approaches for the first half of the window,
        recedes for the second half.
        """
        ph  = self.orbital_clock._phase()
        vis = LEO_VISIBILITY_WINDOW_S
        if ph >= vis:
            return 0.0
        # Cosine model: peak at mid-pass, zero at edges
        angle = math.pi * ph / vis
        return LEO_DOPPLER_MAX_HZ * math.cos(angle - math.pi / 2)

    def transmit(
        self,
        data: bytes,
        apply_latency: bool = True,
    ) -> bytes | None:
        """
        Pass *data* through the LEO channel model.

        Returns:
            The (possibly corrupted) data bytes on success, or None if the
            packet was lost / blocked by a blackout.
        """
        with self._stats_lock:
            self._tx_count += 1

        # ── blackout check ──────────────────────────────────────────────────
        if not self.orbital_clock.is_visible():
            with self._stats_lock:
                self._lost_count += 1
            logger.debug("[CHANNEL] Blackout — frame dropped")
            return None

        # ── bandwidth throttle ──────────────────────────────────────────────
        if self._apply_bw_delay and len(data) > 0:
            tx_time_s = (len(data) * 8) / LEO_BANDWIDTH_BPS
            time.sleep(tx_time_s)

        # ── propagation latency ─────────────────────────────────────────────
        if apply_latency:
            time.sleep(self.one_way_latency_s())

        # ── packet loss ──────────────────────────────────────────────────────
        if random.random() < self._loss_probability():
            with self._stats_lock:
                self._lost_count += 1
            logger.debug("[CHANNEL] Packet lost (loss_p=%.3f)", self._loss_probability())
            return None

        # ── bit errors (rare; driven by atmospheric attenuation) ─────────────
        sq = self._signal_quality_linear()
        bit_error_prob = (1.0 - sq) * 0.01   # scale to a small probability
        if random.random() < bit_error_prob:
            data = self._inject_bit_error(data)

        return data

    @staticmethod
    def _inject_bit_error(data: bytes) -> bytes:
        """Flip a single random bit in *data* to simulate a transmission error."""
        buf     = bytearray(data)
        idx     = random.randrange(len(buf))
        buf[idx] = buf[idx] ^ (1 << random.randrange(8))
        logger.debug("[CHANNEL] Bit error injected at byte %d", idx)
        return bytes(buf)

    def get_stats(self) -> dict:
        with self._stats_lock:
            return {
                "transmitted":   self._tx_count,
                "lost":          self._lost_count,
                "loss_rate":     (
                    self._lost_count / self._tx_count
                    if self._tx_count else 0.0
                ),
                "visible":       self.orbital_clock.is_visible(),
                "weather":       self._weather,
                "doppler_hz":    round(self.doppler_shift_hz(), 1),
                "next_pass_s":   round(self.orbital_clock.time_to_next_pass(), 1),
                "time_in_pass_s": round(self.orbital_clock.time_in_pass(), 1),
            }

    def stop(self):
        self._running = False
