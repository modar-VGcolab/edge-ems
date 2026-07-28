"""configure_logging wires LoggingConfig into the stdlib logging module
(KNOWN_ISSUES #5) -- covers the level and the optional rotating file handler.
"""

import logging
import logging.handlers

from common.config_models import LoggingConfig
from common.logging_setup import configure_logging


def test_level_is_applied_to_root_logger():
    configure_logging(LoggingConfig(level="DEBUG"))
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    configure_logging(LoggingConfig(level="WARNING"))
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_stream_handler_always_present():
    configure_logging(LoggingConfig(level="INFO"))
    handlers = logging.getLogger().handlers
    assert any(isinstance(h, logging.StreamHandler) for h in handlers)


def test_file_handler_added_only_when_file_configured(tmp_path):
    log_path = tmp_path / "edge-ems.log"
    configure_logging(LoggingConfig(level="INFO", file=str(log_path), max_file_size=1000,
                                     backup_count=2))
    handlers = logging.getLogger().handlers
    file_handlers = [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 1000
    assert file_handlers[0].backupCount == 2


def test_repeated_calls_do_not_duplicate_handlers():
    configure_logging(LoggingConfig(level="INFO"))
    configure_logging(LoggingConfig(level="INFO"))
    stream_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, logging.StreamHandler)
    ]
    assert len(stream_handlers) == 1
