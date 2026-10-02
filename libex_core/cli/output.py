"""
What the CLI writes, and where. Data is JSON on standard output; logs and the
final error line go to standard error; nothing is ever colored.
"""

import json
import logging
import re
import sys
from typing import Any

LOGGER_NAME = "libex"

_CONTROLS = re.compile("[\x00-\x1f\x7f-\x9f]")


def escape_controls(text: str) -> str:
    """C0 and C1 controls, newlines included, become \\xNN so nothing a
    remote party influenced can move the cursor or forge a second line."""
    return _CONTROLS.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def error_line(message: str, code: str) -> str:
    return f"libex-core: error: {escape_controls(message)} (code: {code})"


def _plain(value: Any) -> Any:
    # Duck-typed so importing this module does not import pydantic.
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def emit_text(text: str) -> None:
    """Bytes rather than text so the platform's newline translation cannot
    turn a script's line endings into CRLF."""
    sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))


def emit_json(value: Any) -> None:
    """Pretty on a terminal, compact when piped."""
    if sys.stdout.isatty():
        text = json.dumps(_plain(value), ensure_ascii=False, indent=2)
    else:
        text = json.dumps(_plain(value), ensure_ascii=False, separators=(",", ":"))
    emit_text(text + "\n")


class _StderrFormatter(logging.Formatter):
    def formatMessage(self, record: logging.LogRecord) -> str:
        # The traceback appended after this is multi-line by design and is
        # left alone; only the message text is untrusted.
        record.message = escape_controls(record.message)
        return super().formatMessage(record)


_installed: tuple[logging.Handler, int] | None = None


def attach_stderr_logging(level: int) -> None:
    """Attached to the library's own logger and nothing else, so an
    embedding application's root logging is never reconfigured. Safe to
    call twice."""
    global _installed
    detach_stderr_logging()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_StderrFormatter("libex-core: %(levelname)s: %(message)s"))
    logger = logging.getLogger(LOGGER_NAME)
    _installed = (handler, logger.level)
    logger.addHandler(handler)
    logger.setLevel(level)


def detach_stderr_logging() -> None:
    global _installed
    if _installed is None:
        return
    handler, previous_level = _installed
    _installed = None
    logger = logging.getLogger(LOGGER_NAME)
    logger.removeHandler(handler)
    logger.setLevel(previous_level)
