"""
log_safety: the value-safety rule, the incident-window predicate and the ASIN
redaction that keep caller text and malformed upstream values off log lines.
"""

# Third party
import pytest

# Core
from libex_core.log_safety import (
    _MAX_VALUE_LENGTH,
    is_safe_log_value,
    safe_asin_for_log,
    window_elapsed,
)


# ============================================================================
# is_safe_log_value: accepted vocabulary
# ============================================================================

@pytest.mark.parametrize("value", [
    "us",
    "12345",
    "Science Fiction",
    "Science Fiction & Fantasy",           # "&" (Po)
    "SF・ファンタジー",                      # jp middle dot (Po)
    "Littérature d’aventure",         # U+2019 (Pf)
    "पुस्तक",  # Devanagari with combining marks (Mn/Mc)
    "Фантастика",
    "科幻小说",
    "ファンタジー",
    "رواية",
    "นิยาย",      # Thai with marks
    "Kids　Books",                     # ideographic space (Zs)
    "C++ $ + ~ ©",                         # symbols
    "Self-Help (2nd ed.)",
    "",                                    # vacuously safe
])
def test_accepts_catalogue_vocabulary(value):
    assert is_safe_log_value(value) is True


def test_accepts_exactly_max_length():
    assert is_safe_log_value("a" * _MAX_VALUE_LENGTH) is True


def test_rejects_one_past_max_length():
    assert is_safe_log_value("a" * (_MAX_VALUE_LENGTH + 1)) is False


def test_max_length_is_64():
    assert _MAX_VALUE_LENGTH == 64


def test_length_counts_characters_not_bytes():
    # 64 three-byte characters is 192 bytes but within the bound.
    assert is_safe_log_value("科" * 64) is True
    assert is_safe_log_value("科" * 65) is False


# ============================================================================
# is_safe_log_value: rejected characters
# ============================================================================

@pytest.mark.parametrize("ch", [
    "\x00", "\n", "\r", "\t", "\x1b", "\x7f",   # C0 and DEL (Cc)
    "\x85", "\x9f",                             # C1 (Cc)
    " ", " ",                         # line/paragraph separators (Zl/Zp)
    "‮", "‪", "⁦",               # bidi overrides/isolates (Cf)
    "​", "﻿",                         # zero-width space, BOM (Cf)
    "",                                   # private use (Co)
    "\ud800",                                   # lone surrogate (Cs)
    "͸",                                   # unassigned (Cn)
    ";", "=",                                   # query-smuggling characters
])
@pytest.mark.parametrize("template", ["{}", "ab{}", "{}ab", "a{}b"])
def test_rejects_unsafe_character_anywhere(ch, template):
    assert is_safe_log_value(template.format(ch)) is False


def test_rejects_query_smuggled_second_param():
    assert is_safe_log_value("us;Jane Doe") is False
    assert is_safe_log_value("a=b") is False


def test_rejects_log_line_injection():
    assert is_safe_log_value("us\nWARNING forged line") is False


# ============================================================================
# window_elapsed
# ============================================================================

def test_window_elapsed_never_reported_is_due_even_at_clock_zero():
    assert window_elapsed(None, 0.0, 60.0) is True
    assert window_elapsed(None, 5.0, 60.0) is True


def test_window_elapsed_zero_is_a_real_timestamp_not_never():
    # A 0.0 last_logged means "reported at t=0", not "never reported".
    assert window_elapsed(0.0, 10.0, 60.0) is False
    assert window_elapsed(0.0, 60.0, 60.0) is True


def test_window_elapsed_one_tick_before_boundary_is_not_due():
    assert window_elapsed(100.0, 159.999, 60.0) is False


def test_window_elapsed_exactly_at_boundary_is_due():
    assert window_elapsed(100.0, 160.0, 60.0) is True


def test_window_elapsed_past_boundary_is_due():
    assert window_elapsed(100.0, 160.001, 60.0) is True


def test_window_elapsed_same_instant_not_due():
    assert window_elapsed(100.0, 100.0, 60.0) is False


def test_window_elapsed_zero_interval_always_due():
    assert window_elapsed(100.0, 100.0, 0.0) is True


def test_window_elapsed_clock_going_backwards_not_due():
    assert window_elapsed(100.0, 50.0, 60.0) is False


# ============================================================================
# safe_asin_for_log
# ============================================================================

def test_safe_asin_valid_returned_verbatim():
    assert safe_asin_for_log("B012345678") == "B012345678"
    assert safe_asin_for_log("0008433844") == "0008433844"


def test_safe_asin_lowercase_valid_is_not_uppercased():
    assert safe_asin_for_log("b012345678") == "b012345678"


@pytest.mark.parametrize("asin", [
    "", "short", "B0123456789",            # 11 chars
    "B01234567!", "B012345 78",
    "B012345678\n", "B012345678\nforged",
    "Jane Doe", "../../etc/passwd",
])
def test_safe_asin_invalid_is_redacted(asin):
    assert safe_asin_for_log(asin) == "REDACTED"


@pytest.mark.parametrize("value", [None, 1234567890, 1.5, b"B012345678", ["B012345678"], {}])
def test_safe_asin_non_str_is_redacted(value):
    assert safe_asin_for_log(value) == "REDACTED"
