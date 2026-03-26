"""Pratyabhijna logging configuration.

Call configure_logging(config) once at server startup to initialise the
root logger. Use get_logger(__name__) in each module to obtain a named
logger that inherits the root configuration.

Routing rules (PRATYABHIJNA_ENV):
  dev / prod   → RotatingFileHandler → log_dir/pratyabhijna.log
  test         → StreamHandler → stdout (no MCP transport conflict)
  anything else → StreamHandler → stdout (safe fallback)

Note: dev uses file logging because MCP stdio transport owns stdout.
Writing log lines to stdout would corrupt the JSON-RPC stream.
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pratyabhijna.config import PratyabhijnaConfig

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

# 10 MB per file, 5 rotated backups → max 50 MB on disk
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def configure_logging(config: "PratyabhijnaConfig") -> None:
    """Configure the root logger based on the Pratyabhijna config.

    Safe to call multiple times (each call adds exactly one handler).
    In tests, use the isolated_root_logger fixture to prevent leakage.
    """
    if config.env in ("dev", "prod"):
        log_dir = Path(config.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_dir / "pratyabhijna.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    else:
        handler = logging.StreamHandler(sys.stdout)

    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level.upper()))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger for use within a module.

    Example:
        log = get_logger(__name__)
        log.info("Server started")
    """
    return logging.getLogger(name)
