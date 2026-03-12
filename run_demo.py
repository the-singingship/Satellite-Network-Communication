"""
run_demo.py — Launch all 5 TWCP processes for a live demonstration.

Usage:
    python run_demo.py [--time-scale N]

Press Ctrl-C to stop all processes.

Processes started:
    1. Turbine Sensor Emulator  (UDP, port 5001)
    2. Turbine Actuator Ctrl    (TCP, port 5002)
    3. LEO Satellite Relay      (UDP, port 5003)
    4. Ground Control Station   (TCP+UDP, port 5004) — non-interactive
    5. Discovery Service        (UDP broadcast, port 5005)
"""

import argparse
import multiprocessing
import signal
import sys
import time
import os

# ── Process entry points ───────────────────────────────────────────────────────

def run_sensor(relay_port, interval):
    from turbine.sensor_emulator import SensorEmulator
    from utils.logger import setup_logging
    setup_logging("sensor_emulator")
    em = SensorEmulator(relay_port=relay_port, interval=interval)
    em.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        em.stop()


def run_actuator():
    from turbine.actuator_controller import ActuatorController
    from utils.logger import setup_logging
    setup_logging("actuator_controller")
    ctrl = ActuatorController()
    ctrl.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ctrl.stop()


def run_relay(time_scale):
    from satellite.leo_relay import LEORelay
    from utils.logger import setup_logging
    setup_logging("leo_relay")
    relay = LEORelay(time_scale=time_scale)
    relay.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        relay.stop()


def run_ground():
    from ground.control_station import GroundControlStation
    from utils.logger import setup_logging
    setup_logging("control_station")
    gcs = GroundControlStation()
    gcs.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        gcs.stop()


def run_discovery():
    from discovery.discovery_service import DiscoveryService
    from utils.logger import setup_logging
    setup_logging("discovery_service")
    svc = DiscoveryService()
    svc.start()
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        svc.stop()


# ── Demo orchestrator ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TWCP Demo — Launch all 5 processes")
    parser.add_argument(
        "--time-scale", type=float, default=60.0,
        help="Orbital simulation speed multiplier (default: 60 = 1 min/real-s)"
    )
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Sensor reading interval in seconds (default: 2)"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  TWCP — Turbine Wind Control Protocol")
    print("  CSU33D03 Main Project Demo")
    print("=" * 60)
    print()
    print(f"  Orbital time-scale : {args.time_scale}×")
    print(f"  Sensor interval    : {args.interval}s")
    print()
    print("  Starting processes:")
    print("    [1] Turbine Sensor Emulator  (UDP  :5001)")
    print("    [2] Turbine Actuator Ctrl    (TCP  :5002)")
    print("    [3] LEO Satellite Relay      (UDP  :5003)")
    print("    [4] Ground Control Station   (UDP  :5004)")
    print("    [5] Discovery Service        (UDP  :5005)")
    print()
    print("  Logs → logs/ directory")
    print("  Press Ctrl-C to stop all processes.")
    print("=" * 60)
    print()

    processes = [
        multiprocessing.Process(
            target=run_relay,
            args=(args.time_scale,),
            name="LEO-Relay",
            daemon=True,
        ),
        multiprocessing.Process(
            target=run_actuator,
            name="Actuator",
            daemon=True,
        ),
        multiprocessing.Process(
            target=run_ground,
            name="Ground",
            daemon=True,
        ),
        multiprocessing.Process(
            target=run_discovery,
            name="Discovery",
            daemon=True,
        ),
        multiprocessing.Process(
            target=run_sensor,
            args=(5003, args.interval),
            name="Sensor",
            daemon=True,
        ),
    ]

    for p in processes:
        p.start()
        print(f"  ✓ Started {p.name} (pid={p.pid})")
        time.sleep(0.3)   # stagger startup slightly

    print()
    print("  All processes running. Ctrl-C to stop.")
    print()

    def _shutdown(sig, frame):
        print("\n  Shutting down…")
        for p in processes:
            if p.is_alive():
                p.terminate()
        for p in processes:
            p.join(timeout=3)
        print("  Done.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        time.sleep(5)
        alive = sum(1 for p in processes if p.is_alive())
        print(f"  [demo] {alive}/{len(processes)} processes alive", flush=True)


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)
    main()
