"""
protocol/message_types.py — TWCP message type constants and helpers.
"""

# ── Message type codes ────────────────────────────────────────────────────────

# Discovery / negotiation (0x01–0x0F)
MSG_DISCOVER        = 0x01
MSG_NEGOTIATE       = 0x02
MSG_AGREE           = 0x03
MSG_REJECT          = 0x04

# Sensor data (0x10–0x1F)
MSG_SENSOR_DATA     = 0x10
MSG_SENSOR_BATCH    = 0x11

# Control commands (0x20–0x2F)
MSG_YAW_CMD         = 0x20
MSG_PITCH_CMD       = 0x21
MSG_EMERGENCY_STOP  = 0x22

# Acknowledgements (0x30–0x3F)
MSG_ACK             = 0x30
MSG_NACK            = 0x31

# Status / heartbeat (0x40–0x4F)
MSG_HEARTBEAT       = 0x40
MSG_STATUS          = 0x41

# Bonus: video (0x50–0x5F)
MSG_VIDEO_FRAME     = 0x50

# Bonus: security / key exchange (0x60–0x6F)
MSG_KEY_EXCHANGE    = 0x60

# ── Flag bit masks ────────────────────────────────────────────────────────────
FLAG_RELIABLE       = 0x01   # Sender expects ACK
FLAG_ENCRYPTED      = 0x02   # Payload is encrypted
FLAG_FRAGMENTED     = 0x04   # Part of a fragmented stream
FLAG_LAST_FRAG      = 0x08   # Last fragment in a stream
FLAG_PRIORITY       = 0x10   # High-priority frame

# ── Human-readable lookup tables ─────────────────────────────────────────────
MSG_TYPE_NAMES = {
    MSG_DISCOVER:       "DISCOVER",
    MSG_NEGOTIATE:      "NEGOTIATE",
    MSG_AGREE:          "AGREE",
    MSG_REJECT:         "REJECT",
    MSG_SENSOR_DATA:    "SENSOR_DATA",
    MSG_SENSOR_BATCH:   "SENSOR_BATCH",
    MSG_YAW_CMD:        "YAW_CMD",
    MSG_PITCH_CMD:      "PITCH_CMD",
    MSG_EMERGENCY_STOP: "EMERGENCY_STOP",
    MSG_ACK:            "ACK",
    MSG_NACK:           "NACK",
    MSG_HEARTBEAT:      "HEARTBEAT",
    MSG_STATUS:         "STATUS",
    MSG_VIDEO_FRAME:    "VIDEO_FRAME",
    MSG_KEY_EXCHANGE:   "KEY_EXCHANGE",
}


def type_name(code: int) -> str:
    """Return the human-readable name for a message type code."""
    return MSG_TYPE_NAMES.get(code, f"UNKNOWN(0x{code:02X})")
