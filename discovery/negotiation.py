"""
discovery/negotiation.py — TWCP capability negotiation helper.

Builds, sends, and parses NEGOTIATE / AGREE / REJECT frames.
Used by both the discovery service and other nodes during handshake.
"""

import json
import logging

from protocol.frame import build_frame, parse_frame
from protocol.message_types import MSG_NEGOTIATE, MSG_AGREE, MSG_REJECT

logger = logging.getLogger(__name__)


def build_negotiate_payload(node_id: str, session_id: str, capabilities: dict) -> bytes:
    """Return JSON-encoded NEGOTIATE payload."""
    return json.dumps({
        "node_id":      node_id,
        "session_id":   session_id,
        "capabilities": capabilities,
    }).encode()


def build_agree_payload(node_id: str, session_id: str, agreed: dict) -> bytes:
    """Return JSON-encoded AGREE payload."""
    return json.dumps({
        "node_id":    node_id,
        "session_id": session_id,
        "agreed":     agreed,
    }).encode()


def build_reject_payload(reason: str) -> bytes:
    return json.dumps({"reason": reason}).encode()


def negotiate_capabilities(mine: dict, theirs: dict) -> dict:
    """
    Compute the agreed intersection of two capability dictionaries.

    - For list values: take the set intersection.
    - For scalar values: only include if both sides match.
    - For boolean values: only include if both sides are True.
    """
    agreed: dict = {}
    for key, my_val in mine.items():
        if key not in theirs:
            continue
        their_val = theirs[key]
        if isinstance(my_val, list) and isinstance(their_val, list):
            common = list(set(my_val) & set(their_val))
            if common:
                agreed[key] = sorted(common)
        elif isinstance(my_val, bool) and isinstance(their_val, bool):
            if my_val and their_val:
                agreed[key] = True
        elif my_val == their_val:
            agreed[key] = my_val
    return agreed


def parse_negotiate(frame: dict) -> dict | None:
    """Parse a NEGOTIATE frame payload. Returns dict or None."""
    if frame.get("type") != MSG_NEGOTIATE:
        return None
    try:
        return json.loads(frame["payload"])
    except (json.JSONDecodeError, KeyError):
        logger.warning("[NEG] Malformed NEGOTIATE payload")
        return None


def parse_agree(frame: dict) -> dict | None:
    """Parse an AGREE frame payload. Returns dict or None."""
    if frame.get("type") != MSG_AGREE:
        return None
    try:
        return json.loads(frame["payload"])
    except (json.JSONDecodeError, KeyError):
        logger.warning("[NEG] Malformed AGREE payload")
        return None
