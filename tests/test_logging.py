"""Tests for Pratyabhijna logging configuration.

Verifies that configure_logging sets up the correct handler based on
PRATYABHIJNA_ENV, routes to the correct destination, and formats messages
with timestamp and log level before the message.
"""

import logging
import logging.handlers
import sys
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_root_logger():
    """Restore root logger state after each test.

    configure_logging modifies global state; this fixture ensures each
    test starts clean and leaves no residue for subsequent tests.
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield root
    for handler in root.handlers:
        if handler not in original_handlers:
            handler.close()
    root.handlers = original_handlers
    root.level = original_level


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Clear PRATYABHIJNA_* env vars so explicit ``env=`` init kwargs win.

    PratyabhijnaConfig is a pydantic-settings BaseSettings whose
    ``settings_customise_sources`` puts env vars *before* init kwargs.
    The CI/test runner sets ``PRATYABHIJNA_ENV=test``, which silently
    overrides ``PratyabhijnaConfig(env="dev")`` and breaks every
    dev/prod-specific logging test. Stripping the prefixed env vars for
    each test in this module makes the explicit init kwarg take effect.
    """
    for key in list(os.environ):
        if key.startswith("PRATYABHIJNA_"):
            monkeypatch.delenv(key, raising=False)


def _new_handler() -> logging.Handler:
    """Return the most recently added handler on the root logger."""
    return logging.getLogger().handlers[-1]


class TestLoggingDestination:
    """configure_logging routes output to the correct destination."""

    def test_dev_env_uses_rotating_file_handler(self, tmp_path):
        """In dev env, a RotatingFileHandler is used (stdout is MCP stdio)."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev", log_dir=str(tmp_path)))

        assert isinstance(_new_handler(), logging.handlers.RotatingFileHandler)

    def test_dev_log_file_is_in_configured_dir(self, tmp_path):
        """In dev env, the log file is created within log_dir."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev", log_dir=str(tmp_path)))

        handler = _new_handler()
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert Path(handler.baseFilename).parent == tmp_path

    def test_test_env_uses_stream_handler(self):
        """In test env, logging goes to stdout (same as dev)."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="test"))

        handler = _new_handler()
        assert isinstance(handler, logging.StreamHandler)
        assert not isinstance(handler, logging.handlers.RotatingFileHandler)

    def test_test_env_logs_to_stdout(self):
        """In test env, the StreamHandler writes to stdout."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="test"))

        assert _new_handler().stream is sys.stdout

    def test_prod_env_uses_rotating_file_handler(self, tmp_path):
        """In prod env, a RotatingFileHandler is added to the root logger."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="prod", log_dir=str(tmp_path)))

        assert isinstance(_new_handler(), logging.handlers.RotatingFileHandler)

    def test_prod_log_file_is_in_configured_dir(self, tmp_path):
        """In prod env, the log file is created within log_dir."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        config = PratyabhijnaConfig(env="prod", log_dir=str(tmp_path))
        assert config.env == "prod"
        configure_logging(PratyabhijnaConfig(env="prod", log_dir=str(tmp_path)))

        handler = _new_handler()
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert Path(handler.baseFilename).parent == tmp_path

    def test_prod_creates_log_dir_if_missing(self, tmp_path):
        """In prod env, configure_logging creates log_dir if it doesn't exist."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        new_dir = tmp_path / "logs" / "pratyabhijna"
        assert not new_dir.exists()

        configure_logging(PratyabhijnaConfig(env="prod", log_dir=str(new_dir)))

        assert new_dir.exists()

    def test_unknown_env_falls_back_to_stdout(self):
        """Any env value other than 'prod' falls back to stdout."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="staging"))

        handler = _new_handler()
        assert isinstance(handler, logging.StreamHandler)
        assert not isinstance(handler, logging.handlers.RotatingFileHandler)


class TestLogFormat:
    """Log records include timestamp and log level before the message."""

    def test_format_includes_timestamp(self):
        """Formatter includes %(asctime)s."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev"))

        assert "%(asctime)s" in _new_handler().formatter._fmt

    def test_format_includes_level(self):
        """Formatter includes %(levelname)s."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev"))

        assert "%(levelname)s" in _new_handler().formatter._fmt

    def test_format_includes_message(self):
        """Formatter includes %(message)s."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev"))

        assert "%(message)s" in _new_handler().formatter._fmt

    def test_timestamp_comes_before_level(self):
        """Timestamp appears before log level in the format string."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev"))

        fmt = _new_handler().formatter._fmt
        assert fmt.index("%(asctime)s") < fmt.index("%(levelname)s")

    def test_level_comes_before_message(self):
        """Log level appears before message in the format string."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev"))

        fmt = _new_handler().formatter._fmt
        assert fmt.index("%(levelname)s") < fmt.index("%(message)s")

    def test_datefmt_is_set(self):
        """A date format string is configured (not left as default)."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev"))

        assert _new_handler().formatter.datefmt is not None


class TestLogLevel:
    """Root logger level is configurable."""

    def test_default_log_level_is_info(self):
        """Default log level is INFO."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev"))

        assert logging.getLogger().level == logging.INFO

    def test_log_level_can_be_debug(self):
        """Log level DEBUG is accepted."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev", log_level="DEBUG"))

        assert logging.getLogger().level == logging.DEBUG

    def test_log_level_can_be_warning(self):
        """Log level WARNING is accepted."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev", log_level="WARNING"))

        assert logging.getLogger().level == logging.WARNING

    def test_log_level_case_insensitive(self):
        """Log level is accepted regardless of case."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.log import configure_logging

        configure_logging(PratyabhijnaConfig(env="dev", log_level="debug"))

        assert logging.getLogger().level == logging.DEBUG


class TestGetLogger:
    """get_logger returns a named logger."""

    def test_get_logger_returns_logger(self):
        """get_logger returns a logging.Logger instance."""
        from pratyabhijna.log import get_logger

        logger = get_logger("pratyabhijna.test")

        assert isinstance(logger, logging.Logger)

    def test_get_logger_uses_given_name(self):
        """get_logger returns a logger with the given name."""
        from pratyabhijna.log import get_logger

        logger = get_logger("pratyabhijna.test.module")

        assert logger.name == "pratyabhijna.test.module"
