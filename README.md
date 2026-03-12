# Satellite Network Communication — TWCP Protocol

**CSU33D03 Main Project 2025-26**

Custom networking protocol simulating communication from a free-floating
offshore wind turbine to a monitoring and control centre on a space station
in Low Earth Orbit (LEO).

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Project Structure](#project-structure)
3. [Protocol Design — TWCP](#protocol-design--twcp)
   - [Frame Format](#frame-format)
   - [Message Types](#message-types)
   - [State Machine](#state-machine)
4. [Channel Model](#channel-model)
5. [Reliability Layer](#reliability-layer)
6. [Discovery & Negotiation](#discovery--negotiation)
7. [Bonus Tasks](#bonus-tasks)
8. [How to Run](#how-to-run)
9. [Running Tests](#running-tests)

---

## System Overview

```
[Wind Turbine]               [LEO Satellite Relay]        [Ground Station]
  Process 1: Sensor           Process 3: leo_relay          Process 4: Ground Ctrl
  (UDP :5001)    ─────────►  (UDP :5003) ──────────────►   (UDP :5004)
                                                            Process 2: Actuator
  Process 2: Actuator         (applies channel model)       (TCP :5002)
  (TCP :5002)    ◄─────────  (jitter, loss, windows)  ◄───

                                              Process 5: Discovery
                                              (UDP broadcast :5005)
```

Five processes communicate using the custom **TWCP (Turbine Wind Control Protocol)**:

| # | Process | Port | Socket |
|---|---------|------|--------|
| 1 | Turbine Sensor Emulator | 5001 | UDP (raw socket) |
| 2 | Turbine Actuator Controller | 5002 | TCP (raw socket) |
| 3 | LEO Satellite Relay | 5003 | UDP |
| 4 | Ground Control Station | 5004 | TCP + UDP |
| 5 | Discovery & Negotiation Service | 5005 | UDP broadcast |

---

## Project Structure

```
Satellite-Network-Communication/
├── README.md
├── requirements.txt           # Empty — stdlib only
├── run_demo.py                # Launch all 5 processes
├── config.py                  # Centralised configuration
│
├── protocol/
│   ├── frame.py               # Custom binary frame: build/parse/CRC
│   ├── message_types.py       # MSG_* constants and flag bits
│   ├── channel_model.py       # LEO channel emulator
│   ├── reliability.py         # Stop-and-Wait + Selective Repeat ARQ
│   └── handshake.py           # 4-step DISCOVER→NEGOTIATE→AGREE→ACK
│
├── turbine/
│   ├── sensor_emulator.py     # Process 1 — UDP, port 5001
│   └── actuator_controller.py # Process 2 — TCP, port 5002
│
├── satellite/
│   └── leo_relay.py           # Process 3 — UDP, port 5003
│
├── ground/
│   └── control_station.py     # Process 4 — TCP+UDP, port 5004
│
├── discovery/
│   ├── discovery_service.py   # Process 5 — UDP broadcast, port 5005
│   └── negotiation.py         # Capability negotiation helpers
│
├── bonus/
│   ├── video_stream.py        # Bonus 1: Video fragmentation/reassembly
│   ├── interop.py             # Bonus 2: Cross-group interop interface
│   └── security.py            # Bonus 3: Auth, replay, anomaly detection
│
├── utils/
│   └── logger.py              # Centralised logging (console + file)
│
└── tests/
    ├── test_frame.py          # Frame build/parse/CRC unit tests
    ├── test_channel.py        # Channel model unit tests
    ├── test_reliability.py    # ARQ unit tests
    └── test_integration.py    # End-to-end protocol flow tests
```

---

## Protocol Design — TWCP

**TWCP (Turbine Wind Control Protocol)** is a completely original binary
protocol designed for this project. It does **not** replicate CCSDS,
MQTT, or any existing space or IoT protocol.

### Frame Format

Every TWCP message is wrapped in a fixed-overhead frame:

```
+--------+-----+------+-------+-----+-----+---------+-------+
| SYNC   | VER | TYPE | FLAGS | SEQ | LEN | PAYLOAD | CRC32 |
| 2 B    | 1 B | 1 B  | 1 B   | 4 B | 2 B |  N B    |  4 B  |
+--------+-----+------+-------+-----+-----+---------+-------+
 Total fixed overhead: 15 bytes (11 header + 4 CRC)
```

| Field | Width | Description |
|-------|-------|-------------|
| SYNC  | 2 B | Synchronisation word `0xAA 0x55` — marks start of frame |
| VER   | 1 B | Protocol version (currently 1) |
| TYPE  | 1 B | Message type code (see below) |
| FLAGS | 1 B | Bit flags: RELIABLE, ENCRYPTED, FRAGMENTED, LAST\_FRAG, PRIORITY |
| SEQ   | 4 B | Sequence number (32-bit, wraps at 2³²) |
| LEN   | 2 B | Payload length in bytes (0–65535) |
| PAYLOAD | N B | JSON or binary payload |
| CRC32 | 4 B | IEEE 802.3 CRC-32 of header+payload |

All multi-byte fields are **big-endian (network byte order)**.
Implementation uses Python `struct` with format `!2sBBBIH`.

### Message Types

```
Discovery / Negotiation  (0x01–0x0F)
  0x01  DISCOVER        Broadcast presence, advertise capabilities
  0x02  NEGOTIATE       Exchange capability details
  0x03  AGREE           Accept negotiated capabilities
  0x04  REJECT          Decline negotiation

Sensor Data              (0x10–0x1F)
  0x10  SENSOR_DATA     Single turbine sensor reading (JSON)
  0x11  SENSOR_BATCH    Bundle of N readings (JSON array)

Control Commands         (0x20–0x2F)
  0x20  YAW_CMD         Set turbine yaw angle
  0x21  PITCH_CMD       Set turbine pitch angle
  0x22  EMERGENCY_STOP  Immediately feather blades

Acknowledgements         (0x30–0x3F)
  0x30  ACK             Positive acknowledgement
  0x31  NACK            Negative acknowledgement (with reason)

Status / Heartbeat       (0x40–0x4F)
  0x40  HEARTBEAT       Keepalive (JSON with timestamp)
  0x41  STATUS          Actuator or system status

Bonus                    (0x50–0x6F)
  0x50  VIDEO_FRAME     Fragmented video frame chunk
  0x60  KEY_EXCHANGE    HMAC challenge/response
```

### State Machine

#### Handshake (4-step):

```
INITIATOR                               RESPONDER
  IDLE                                    IDLE
    │                                       │
    ├──── DISCOVER ────────────────────────►│
    │                                  DISCOVERING
    │     NEGOTIATE ◄──────────────────────┤
  NEGOTIATING                          NEGOTIATING
    │                                       │
    ├──── AGREE ───────────────────────────►│
    │                                    AGREED
    │     ACK ◄────────────────────────────┤
  ESTABLISHED                          ESTABLISHED
```

#### Sensor data (Selective Repeat ARQ):

```
Turbine                                 Ground
  │──── SENSOR_DATA (seq=N, FLAG_RELIABLE) ──►│
  │◄─── ACK (seq=N) ──────────────────────────│
  │──── SENSOR_DATA (seq=N+1) ────────────────►│
  ...  (sliding window, out-of-order buffered)
```

#### Command (Stop-and-Wait ARQ):

```
Ground                                  Actuator
  │──── YAW_CMD (seq=N, FLAG_RELIABLE) ──────►│
  │◄─── ACK/NACK (seq=N, reason) ─────────────│
  [retransmit on timeout, max 5 retries]
```

---

## Channel Model

`protocol/channel_model.py` realistically models the LEO satellite link.

### Parameters

| Parameter | Value | Source |
|-----------|-------|--------|
| One-way base latency | 40 ms | LEO ~350 km altitude |
| Jitter | ±15 ms (Gaussian) | Measured LEO variability |
| Baseline packet loss | 5% | Typical LEO link |
| Pass-edge packet loss | up to 15% | Doppler & elevation angle |
| Bandwidth limit | 256 kbps | Narrow-band LEO transponder |
| Orbital period | 90 min | Standard LEO period |
| Visibility window | 10 min per pass | ~350 km altitude geometry |
| Blackout period | ~80 min per orbit | Between passes |
| Doppler shift (max) | ±50 kHz | Simulated; informational |

### Atmospheric Attenuation

Four weather states modelled with signal degradation (dB):

| State | Attenuation |
|-------|-------------|
| Clear | 0.5 dB |
| Cloudy | 1.5 dB |
| Rainy | 3.0 dB |
| Storm | 6.0 dB |

Attenuation is converted to a bit-error probability and applied to
in-flight frames.

### Blackout Handling

The relay queues frames during blackout periods. Queued frames
older than 120 seconds are discarded to prevent unbounded buffer
growth.

---

## Reliability Layer

`protocol/reliability.py` provides two ARQ modes:

### Stop-and-Wait ARQ

Used for **control commands** (YAW\_CMD, PITCH\_CMD, EMERGENCY\_STOP)
where in-order, reliable delivery is required:

1. Sender transmits frame with `FLAG_RELIABLE`
2. Sender blocks waiting for ACK (timeout: 2 s, max retries: 5)
3. On ACK: advance seq, return success
4. On NACK/timeout: retransmit same seq
5. After max retries: return failure

### Selective Repeat ARQ

Used for **sensor data streams** where throughput matters:

1. Sender maintains a window of 8 outstanding frames
2. Each frame is ACKed individually by the receiver
3. Out-of-order frames are buffered; in-order delivery to application
4. Timed-out frames are selectively retransmitted (not the whole window)
5. A retransmit timer thread calls `retransmit_expired()` periodically

---

## Discovery & Negotiation

`discovery/discovery_service.py` runs on UDP broadcast port 5005.

### Protocol Flow

```
Node A (broadcast)       Network              Node B
    │                                           │
    ├─── DISCOVER (broadcast) ─────────────────►│
    │                                           │
    │◄── NEGOTIATE (unicast) ───────────────────┤
    │                                           │
    ├─── AGREE (unicast) ──────────────────────►│
    │                                           │
    │◄── ACK ───────────────────────────────────┤
    │                                           │
  [session established, peers registered]
```

### Capability Advertisement

Each DISCOVER payload includes a JSON capability block:

```json
{
  "node_id":      "TURBINE-001",
  "capabilities": {
    "protocol":   "TWCP",
    "version":    "1",
    "sensor_types": ["wind_speed", "yaw", "pitch", "rpm", "power", "temp"],
    "commands":   ["YAW_CMD", "PITCH_CMD", "EMERGENCY_STOP"],
    "reliability": ["STOP_AND_WAIT", "SELECTIVE_REPEAT"],
    "video":      false,
    "security":   true
  }
}
```

Negotiation computes the **intersection** of capabilities from both sides.

---

## Bonus Tasks

### Bonus 1: Video Streaming (`bonus/video_stream.py`)

- `VideoStreamSender` synthesises fake compressed frames and
  fragments them into ≤1024-byte TWCP `VIDEO_FRAME` messages
- Each fragment includes a header: `video_id | frag_idx | total_frags | width`
- `VideoStreamReceiver` buffers out-of-order fragments and reassembles
- `AdaptiveBitrateController` adjusts bitrate based on channel loss rate:
  - loss > 15% → reduce to 70% of current bitrate (min 128 kbps)
  - loss < 3%  → increase to 120% of current bitrate (max 2048 kbps)

### Bonus 2: Interoperation (`bonus/interop.py`)

Clean JSON-based interoperation interface for cross-group communication:

```json
{
  "schema":       "1.0",
  "protocol":     "TWCP",
  "version":      "1",
  "node_id":      "TURBINE-001",
  "group":        "CSU33D03-Group",
  "listen_port":  5005,
  "capabilities": { ... }
}
```

- `InteropBeacon` broadcasts plain-JSON DISCOVER messages (no TWCP framing)
  every 30 s so foreign implementations can discover this node
- `InteropListener` parses plain-JSON messages from foreign nodes and
  validates against the schema

### Bonus 3: Security (`bonus/security.py`)

| Feature | Implementation |
|---------|----------------|
| Challenge-response auth | HMAC-SHA256 with pre-shared key |
| Replay detection | Sliding window (30 s) on (source, seq) pairs |
| Anomaly detection | Physics limits + rate-of-change checks per source |
| Rate limiting | Token bucket, 5 commands/second per source |

The `SecurityGate` class chains all three checks. Any failure causes the
frame to be silently dropped and logged.

---

## How to Run

### Prerequisites

- Python 3.10+ (standard library only — no pip installs needed)
- Linux / macOS / Windows

### Quick Start (all processes at once)

```bash
python run_demo.py
```

Optional flags:

```
--time-scale N   Orbital simulation speed (default: 60 = 1 min/real-second)
--interval S     Sensor reading interval in seconds (default: 2.0)
```

### Running Individual Processes

```bash
# Terminal 1 — LEO Relay (start first)
python -m satellite.leo_relay --time-scale 60

# Terminal 2 — Turbine Actuator Controller
python -m turbine.actuator_controller

# Terminal 3 — Ground Control Station (interactive CLI)
python -m ground.control_station

# Terminal 4 — Discovery Service
python -m discovery.discovery_service

# Terminal 5 — Turbine Sensor Emulator
python -m turbine.sensor_emulator
```

### Ground Control CLI Commands

Once `ground/control_station.py` is running:

```
gcs> yaw 270       # Set turbine yaw to 270°
gcs> pitch 15      # Set turbine pitch to 15°
gcs> estop         # Emergency stop
gcs> status        # Show latest sensor reading
gcs> stats         # Show link quality statistics
gcs> help          # Show all commands
gcs> quit          # Exit
```

### Logs

All processes write logs to the `logs/` directory:
- `logs/sensor_emulator.log`
- `logs/actuator_controller.log`
- `logs/leo_relay.log`
- `logs/control_station.log`
- `logs/discovery_service.log`

---

## Running Tests

```bash
python -m unittest discover -s tests -v
```

Test coverage:

| Test file | Coverage |
|-----------|----------|
| `test_frame.py` | Frame build, parse, CRC validation, edge cases |
| `test_channel.py` | Channel model, orbital clock, visibility |
| `test_reliability.py` | Stop-and-Wait ARQ, Selective Repeat ARQ |
| `test_integration.py` | End-to-end sensor data, commands, handshake, security |

---

## Design Decisions

1. **CRC-32 via `zlib.crc32`** rather than `hashlib.md5` — faster and
   standard for frame integrity (not security); avoids a full hash per frame.
2. **Big-endian framing** (`!` format) — consistent with traditional
   network protocols; makes Wireshark/hex-dump inspection easy.
3. **Session ID from responder** — the NEGOTIATE reply carries the
   session_id so both sides use the same value regardless of which side
   generated it.
4. **Blackout queuing** — the relay queues frames during LEO blackout with
   a 120 s max age to balance buffering and freshness of data.
5. **Time-scale parameter** — the orbital simulation supports a speed
   multiplier so the 90-min orbit can be demonstrated in seconds/minutes.

