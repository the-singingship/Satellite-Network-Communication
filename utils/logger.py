"""
utils/logger.py — Centralised logging configuration for the TWCP system.
"""

import logging
import os
import sys

from config import LOG_LEVEL, LOG_DIR


_LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)-28s  %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(process_name: str = "twcp", log_to_file: bool = True) -> logging.Logger:
    """
    Configure root logging for *process_name*.

    - Console handler: always attached (INFO level unless LOG_LEVEL=DEBUG).
    - File handler: ``logs/<process_name>.log`` (all levels).
    - Returns the named logger for *process_name*.
    """
    global _configured
    if _configured:
        return logging.getLogger(process_name)

    level = getattr(logging, LOG_LEVEL.upper(), logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    # File
    if log_to_file:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(os.path.join(LOG_DIR, f"{process_name}.log"))
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        root.addHandler(fh)

    _configured = True
    return logging.getLogger(process_name)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger (assumes setup_logging has been called)."""
    return logging.getLogger(name)
