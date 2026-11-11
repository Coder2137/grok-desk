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
    """Register a log file. The directory is chosen when a line is written.

    Agents call this at import, before desk 2 sets LOG_DIR. Opening the file
    here would pin desk 2 to logs/. Desk 1 leaves LOG_DIR unset and still
    writes logs/<filename>.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger._desk_filename = filename
    return logger


def _bind(logger: logging.Logger) -> None:
    filename = getattr(logger, "_desk_filename", None)
    if not filename:
        return
    path = Path(os.environ.get("LOG_DIR") or "logs") / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    target = str(path.resolve())
    for handler in list(logger.handlers):
        if getattr(handler, "_desk_path", None) == target:
            return
        if getattr(handler, "_desk_filename", None) == filename:
            logger.removeHandler(handler)
            handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._desk_path = target
    handler._desk_filename = filename
    logger.addHandler(handler)


def log(logger: logging.Logger, message: str) -> None:
    _bind(logger)
    logger.info(redact(message))
