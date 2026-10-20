# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import logging
import logging.config
import os


def configure_logging(level: str | None = None) -> None:
    resolved_level = (level or os.environ.get("MAILINTEL_LOG_LEVEL") or "INFO").upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stderr",
                }
            },
            "loggers": {
                "mailintel": {
                    "handlers": ["console"],
                    "level": resolved_level,
                    "propagate": False,
                }
            },
        }
    )
