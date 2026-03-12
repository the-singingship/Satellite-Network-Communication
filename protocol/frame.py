"""
protocol/frame.py — Custom TWCP binary frame: build, parse, and validate.

Frame layout (all multi-byte fields are big-endian):
  +--------+-----+------+-------+-----+-----+---------+-------+
  | SYNC   | VER | TYPE | FLAGS | SEQ | LEN | PAYLOAD | CRC32 |
  | 2 B    | 1 B | 1 B  | 1 B   | 4 B | 2 B | N B     | 4 B   |
  +--------+-----+------+-------+-----+-----+---------+-------+

Total overhead per frame = 11 (header) + 4 (CRC) = 15 bytes.
"""

import struct
import zlib

from config import TWCP_SYNC_WORD, TWCP_VERSION

# struct format for the fixed header (11 bytes)
_HEADER_FMT   = "!2sBBBIH"
_HEADER_BYTES = struct.calcsize(_HEADER_FMT)   # == 11
_CRC_FMT      = "!I"
_CRC_BYTES    = struct.calcsize(_CRC_FMT)       # == 4


def _crc32(data: bytes) -> int:
    """Return unsigned 32-bit CRC of *data*."""
    return zlib.crc32(data) & 0xFFFFFFFF


def build_frame(msg_type: int, seq: int, payload: bytes, flags: int = 0) -> bytes:
    """
    Pack a TWCP frame.

    Args:
        msg_type: One of the MSG_* constants from message_types.py.
        seq:      Sequence number (uint32, wraps at 2^32).
        payload:  Raw bytes to carry.
        flags:    Optional flag bits (FLAG_* from message_types.py).

    Returns:
        Complete frame as bytes.
    """
    if len(payload) > 0xFFFF:
        raise ValueError(f"Payload too large: {len(payload)} bytes (max 65535)")

    header = struct.pack(
        _HEADER_FMT,
        TWCP_SYNC_WORD,
        TWCP_VERSION,
        msg_type & 0xFF,
        flags & 0xFF,
        seq & 0xFFFFFFFF,
        len(payload),
    )
    body = header + payload
    crc  = struct.pack(_CRC_FMT, _crc32(body))
    return body + crc


def parse_frame(raw: bytes) -> dict:
    """
    Unpack and validate a TWCP frame.

    Returns a dict with keys:
        valid   bool   – False if sync/CRC check fails
        version int
        type    int    – message type code
        flags   int
        seq     int
        payload bytes
        error   str    – human-readable reason when valid is False
    """
    result = {
        "valid":   False,
        "version": 0,
        "type":    0,
        "flags":   0,
        "seq":     0,
        "payload": b"",
        "error":   "",
    }

    min_len = _HEADER_BYTES + _CRC_BYTES
    if len(raw) < min_len:
        result["error"] = f"Frame too short: {len(raw)} < {min_len}"
        return result

    sync, ver, mtype, flags, seq, plen = struct.unpack_from(_HEADER_FMT, raw, 0)

    if sync != TWCP_SYNC_WORD:
        result["error"] = f"Bad sync: {sync!r}"
        return result

    total_expected = _HEADER_BYTES + plen + _CRC_BYTES
    if len(raw) < total_expected:
        result["error"] = (
            f"Truncated frame: got {len(raw)}, need {total_expected}"
        )
        return result

    payload           = raw[_HEADER_BYTES : _HEADER_BYTES + plen]
    crc_received,     = struct.unpack_from(_CRC_FMT, raw, _HEADER_BYTES + plen)
    crc_computed      = _crc32(raw[: _HEADER_BYTES + plen])

    if crc_received != crc_computed:
        result["error"] = (
            f"CRC mismatch: got 0x{crc_received:08X}, "
            f"expected 0x{crc_computed:08X}"
        )
        return result

    result.update(
        valid   = True,
        version = ver,
        type    = mtype,
        flags   = flags,
        seq     = seq,
        payload = payload,
        error   = "",
    )
    return result


def frame_size(payload_len: int) -> int:
    """Return the total on-wire size for a given payload length."""
    return _HEADER_BYTES + payload_len + _CRC_BYTES
