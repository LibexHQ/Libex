"""
Unit tests for logging configuration: the context formatter that surfaces
structured `extra` fields, and the LOG_LEVEL resolution.
"""

# Standard library
import logging
from types import SimpleNamespace

# Local
from app.core.logging import ContextFormatter, MaxLevelFilter, _extra_fields, _resolve_level


def _record(msg, level=logging.INFO, **extra):
    record = logging.LogRecord(
        name="libex", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_formatter_appends_extra_fields():
    fmt = ContextFormatter("%(levelname)s - %(message)s")
    out = fmt.format(_record("Scan complete", total_found=1000, new_books=0))
    assert "Scan complete" in out
    assert "total_found: 1000" in out
    assert "new_books: 0" in out


def test_formatter_clean_without_extra():
    fmt = ContextFormatter("%(levelname)s - %(message)s")
    out = fmt.format(_record("Plain message"))
    assert out == "INFO - Plain message"
    assert "(" not in out


def test_extra_fields_excludes_standard_attrs():
    record = _record("hi", found=5)
    extras = _extra_fields(record)
    assert extras == {"found": 5}
    assert "levelname" not in extras
    assert "msg" not in extras


def test_max_level_filter_allows_at_or_below():
    f = MaxLevelFilter(logging.INFO)
    assert f.filter(_record("info", level=logging.INFO)) is True
    assert f.filter(_record("debug", level=logging.DEBUG)) is True


def test_max_level_filter_blocks_above():
    f = MaxLevelFilter(logging.INFO)
    assert f.filter(_record("warn", level=logging.WARNING)) is False
    assert f.filter(_record("error", level=logging.ERROR)) is False


def test_resolve_level_uses_log_level():
    s = SimpleNamespace(debug=False, log_level="WARNING")
    assert _resolve_level(s) == logging.WARNING


def test_resolve_level_debug_mode_forces_debug():
    s = SimpleNamespace(debug=True, log_level="ERROR")
    assert _resolve_level(s) == logging.DEBUG


def test_resolve_level_is_case_insensitive():
    s = SimpleNamespace(debug=False, log_level="debug")
    assert _resolve_level(s) == logging.DEBUG


def test_resolve_level_unknown_falls_back_to_info():
    s = SimpleNamespace(debug=False, log_level="NONSENSE")
    assert _resolve_level(s) == logging.INFO


def test_resolve_level_defaults_to_info_when_empty():
    s = SimpleNamespace(debug=False, log_level="")
    assert _resolve_level(s) == logging.INFO