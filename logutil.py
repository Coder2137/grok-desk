"""File logs and redaction. Secret env values never land in a log line."""

import logging
import os
from pathlib import Path

_SECRET_ENV = (
    "TYPESAFE_API_KEY",
    "TG_TOKEN",
    "VAPID_PRIVATE_KEY",
    "APP_TOKEN",
    "DATA_PATH",
)


def redact(text: str) -> str:
    if text is None:
        return ""
    out = str(text)
    for name in _SECRET_ENV:
        value = os.environ.get(name) or ""
        if len(value) >= 4:
            out = out.replace(value, "[redacted]")
    return out


def file_logger(name: str, filename: str) -> logging.Logger:
    path = Path("logs") / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    target = str(path.resolve())
    if not any(getattr(h, "_desk_path", None) == target for h in logger.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._desk_path = target
        logger.addHandler(handler)
    return logger


def log(logger: logging.Logger, message: str) -> None:
    logger.info(redact(message))
