"""Wire the `logging:` block of edge_ems_config.*.yaml into Python's logging
module (KNOWN_ISSUES #5).

Both `core.main` and `controller.main` call `configure_logging(ec.logging)`
once at startup, right after their config loads -- before this fix, the field
was parsed and validated but nothing ever read it, so every deployment ran
with whatever logging (or lack of it) happened to be the process default.
"""

from __future__ import annotations

import logging
import logging.handlers

from common.config_models import LoggingConfig

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(cfg: LoggingConfig) -> None:
    """Configure the root logger from a validated `LoggingConfig`.

    `cfg.level` sets the root logger's level (and the stream handler's).
    When `cfg.file` is set, a `RotatingFileHandler` is added alongside the
    stream handler (both active — stdout for `docker logs`/systemd journal,
    the file for anything that wants on-disk history); `max_file_size` and
    `backup_count` fall back to permissive defaults (10 MB x 3) if unset so a
    bare `file: /path/to.log` is enough to enable rotation.
    """
    root = logging.getLogger()
    root.setLevel(cfg.level)
    # Idempotent: calling this twice (tests, or a future config-reload path)
    # must not double up handlers on the root logger.
    root.handlers.clear()

    formatter = logging.Formatter(_FORMAT)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if cfg.file:
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.file,
            maxBytes=cfg.max_file_size or 10_000_000,
            backupCount=cfg.backup_count or 3,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
