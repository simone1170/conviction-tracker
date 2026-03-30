"""Rotating file logger shared by all modules."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import LOG_FILE, LOG_LEVEL

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(module)s | %(message)s"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 3


def get_logger(name: str) -> logging.Logger:
    """Return a named logger writing to the rotating log file and stdout.

    Args:
        name: Logger name (typically __name__ of the calling module).

    Returns:
        Configured Logger instance.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    formatter = logging.Formatter(_LOG_FORMAT)

    # File handler
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger
