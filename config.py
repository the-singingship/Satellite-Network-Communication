"""
config.py — Centralized configuration for the TWCP satellite network system.
"""

# ── Port assignments ──────────────────────────────────────────────────────────
TURBINE_SENSOR_PORT    = 5001   # Process 1: Turbine sensor emulator  (UDP)
TURBINE_ACTUATOR_PORT  = 5002   # Process 2: Turbine actuator ctrl    (TCP)
LEO_RELAY_PORT         = 5003   # Process 3: LEO satellite relay      (UDP)
GROUND_CONTROL_PORT    = 5004   # Process 4: Ground control station   (TCP)
DISCOVERY_PORT         = 5005   # Process 5: Discovery / negotiation  (UDP broadcast)

# ── Host addresses ────────────────────────────────────────────────────────────
LOCALHOST              = "127.0.0.1"
BROADCAST_ADDR         = "255.255.255.255"

# ── Protocol constants ────────────────────────────────────────────────────────
TWCP_SYNC_WORD         = b'\xAA\x55'   # Frame synchronisation marker
TWCP_VERSION           = 1
# Header layout: SYNC(2) VER(1) TYPE(1) FLAGS(1) SEQ(4) LEN(2) = 11 bytes
# Footer:        CRC32(4)
# Total frame overhead = 15 bytes
TWCP_HEADER_FMT        = "!2sBBBIH"   # big-endian
TWCP_HEADER_BYTES      = 11
TWCP_CRC_BYTES         = 4
TWCP_FRAME_OVERHEAD    = TWCP_HEADER_BYTES + TWCP_CRC_BYTES   # 15 bytes

# ── Channel model parameters ──────────────────────────────────────────────────
LEO_BASE_LATENCY_MS    = 40.0          # one-way propagation delay
LEO_JITTER_MS          = 15.0          # Gaussian std-dev
LEO_PACKET_LOSS_RATE   = 0.05          # 5 % baseline
LEO_BANDWIDTH_BPS      = 256_000       # 256 kbps
LEO_ORBITAL_PERIOD_S   = 90 * 60       # 5 400 s ≈ 90 min
LEO_VISIBILITY_WINDOW_S = 10 * 60      # 600 s ≈ 10 min
LEO_DOPPLER_MAX_HZ     = 50_000        # max frequency shift (Hz) — simulated
LEO_ATTENUATION_DB_CLEAR  = 0.5        # clear-sky path attenuation (dB)
LEO_ATTENUATION_DB_STORM  = 6.0        # heavy-rain attenuation (dB)

# ── Reliability layer ─────────────────────────────────────────────────────────
ARQ_TIMEOUT_S          = 2.0
ARQ_MAX_RETRIES        = 5
SR_WINDOW_SIZE         = 8             # Selective Repeat window

# ── Sensor emulator ───────────────────────────────────────────────────────────
SENSOR_INTERVAL_S      = 2.0
TURBINE_ID             = "TURBINE-001"

# ── Security ──────────────────────────────────────────────────────────────────
CHALLENGE_LENGTH       = 16            # bytes for HMAC challenge
RATE_LIMIT_CMDS_PER_S  = 5             # max commands per second per source
REPLAY_WINDOW_S        = 30            # seconds — replay detection window

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL              = "DEBUG"
LOG_DIR                = "logs"

# ── Interoperability ──────────────────────────────────────────────────────────
INTEROP_VERSION        = "1.0"
INTEROP_PROTOCOL_NAME  = "TWCP"
