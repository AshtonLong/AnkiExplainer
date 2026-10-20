"""Add-on logging.

Logs go to ``user_files/explainer.log`` (rotated). Every record passes through a
redaction filter so API keys can never end up on disk, even if a caller makes a
mistake. Callers must never log card content; log ids, sizes and error codes.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re

_LOGGER_NAME = "anki_explainer"
_KEY_RE = re.compile(r"sk-or-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9_\-\.]{8,}")

_configured = False


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = None
        # Exception text (e.g. a header value inside an error message) too.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def redact(text: str) -> str:
    return _KEY_RE.sub("[REDACTED]", text)


def configure(user_files_dir: str) -> None:
    """Attach the rotating file handler. Safe to call more than once."""
    global _configured
    if _configured:
        return
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        os.makedirs(user_files_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(user_files_dir, "explainer.log"),
            maxBytes=512 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())
    _configured = True


def get_logger(name: str = "") -> logging.Logger:
    logger = logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
