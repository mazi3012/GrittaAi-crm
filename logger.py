"""Gretta AI — Structured logging configuration.

Provides consistent logging across all modules with proper formatting
and level management.
"""
import logging
import sys
import os
from typing import Optional

# Try to import from config, fallback to defaults
try:
    from config import LOG_LEVEL, LOG_FORMAT
except ImportError:
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def setup_logging(name: Optional[str] = None, level: Optional[str] = None) -> logging.Logger:
    """Configure and return a logger instance.
    
    Args:
        name: Logger name (typically __name__)
        level: Log level override (defaults to config.LOG_LEVEL)
    
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name or "gretta")
    
    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger
    
    log_level = (level or LOG_LEVEL).upper()
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    
    # Console handler with formatting
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(LOG_FORMAT)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """Get or create a logger for a module."""
    return setup_logging(name)


# Module-level logger for general use
log = get_logger("gretta")
