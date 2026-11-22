# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [value for value in secrets if value]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


def configure_logging(
    logs_dir: Path,
    *,
    level: int = logging.INFO,
    console: bool = True,
    secrets: list[str] | None = None,
) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("plainbagel")
    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    redaction = SecretRedactingFilter(
        (secrets or [])
        + [
            os.environ.get("IA_ACCESS_KEY", ""),
            os.environ.get("IA_SECRET_KEY", ""),
        ]
    )

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(redaction)
        root.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        logs_dir / "archiver.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction)
    root.addHandler(file_handler)

    logging.getLogger("internetarchive").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    return root


def redact(text: str, secrets: list[str]) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"(Authorization:\s*LOW\s+)[^\s]+", r"\1[REDACTED]", result, flags=re.I)
    return result
