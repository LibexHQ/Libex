"""
The log-value safety rule, kept in libex_core because core code cannot import
from app.
"""

# Standard library
import unicodedata

# Core
from libex_core.asin import is_valid_asin


# An allowlisted key earns its value logged verbatim only if that value looks
# like the short token these params take -- a region code, a number, an enum, a
# facet name in any language.
#
# Judged by Unicode category rather than by a list of permitted characters,
# because the vocabulary being judged is Audible's and it grows without notice.
# A literal list has to be extended for every taxonomy a marketplace adds, and
# the character it misses next is region-specific and invisible from a US test
# run. Measured across all eleven live taxonomies, an ASCII-shaped rule lost
# 83% of top-level genre names in us/uk/ca/au/in to "&" alone, all 402 jp names
# built on "・", the fr and it names spelled with U+2019 instead of the ASCII
# apostrophe, and every Devanagari, Tamil and Thai name whose letters carry a
# combining mark -- those redact on their letters, with no punctuation in them
# at all. Letters, marks, numbers, punctuation, symbols and spaces are what a
# catalogue name is made of in every script, so that is what the rule names.
_SAFE_VALUE_CATEGORIES = frozenset({
    "Lu", "Ll", "Lt", "Lm", "Lo",              # letters, every script
    "Mn", "Mc", "Me",                          # combining marks
    "Nd", "Nl", "No",                          # numbers
    "Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po",  # punctuation
    "Sc", "Sk", "Sm", "So",                    # symbols
    "Zs",                                      # spaces, ASCII and ideographic
})

# Excluded by name from the punctuation and symbol categories above, because
# they are how a whole second query rides inside one value: in "?region=us;
# Jane+Doe" the whole of "us;Jane Doe" is one param to parse_qsl, which has not
# split on ";" since CVE-2021-23336, so without this the typed name is logged
# verbatim.
_UNSAFE_VALUE_CHARS = frozenset(";=")

# The categories the set above leaves out are left out deliberately: the
# control and format categories, which is where CR, LF, NUL and U+0085 live,
# and the line and paragraph separators U+2028 and U+2029. None of them belongs
# in a catalogue name, and any of them would put part of a value on a log line
# of its own. urlencode percent-encodes them downstream regardless; the
# guarantee is held here as well rather than borrowed from a later caller.

# The longest of 6,787 genre names measured across the eleven marketplaces is
# 62 characters, so the bound costs no real vocabulary. It counts decoded
# characters rather than bytes, so a name in a multi-byte script is not charged
# for its encoding.
_MAX_VALUE_LENGTH = 64


def is_safe_log_value(value: str) -> bool:
    """
    True if a value is short catalogue vocabulary rather than caller text.

    Public rather than local to this module: it is the one place the
    value-safety judgment is made, and every other log field that could embed
    caller-supplied text -- a cache key built from a query param, for one --
    calls this rather than growing its own copy of the same rule.
    """
    if len(value) > _MAX_VALUE_LENGTH:
        return False
    return all(
        ch not in _UNSAFE_VALUE_CHARS
        and unicodedata.category(ch) in _SAFE_VALUE_CATEGORIES
        for ch in value
    )


def window_elapsed(last_logged: float | None, now: float, interval: float) -> bool:
    """
    True when a windowed incident report is due: nothing reported yet, or the
    last report is at least `interval` seconds old.

    Shared by the repeat-incident warnings that cap themselves to one line per
    window. Any of them can fire on every product in a page at once when what
    changed is upstream and systematic rather than one product being odd, and a
    line per product buries the only thing worth reading, which is that it
    happened and how often. Each caller keeps its own state and its own fields;
    only this predicate is genuinely the same rule every time.

    None rather than 0.0 for "never reported", and the distinction is not
    cosmetic: time.monotonic() counts from boot, so against a 0.0 sentinel
    this arithmetic reads "never reported" as "reported at boot" and stays
    false for the first interval of a process's life -- swallowing the very
    first report. That is the worst window in which to lose a warning: an
    upstream rename is exactly what these exist to catch, and every worker
    process starts fresh on each deploy.
    """
    if last_logged is None:
        return True
    return now - last_logged >= interval


def safe_asin_for_log(asin: str) -> str:
    """
    Returns an ASIN as-is for logging if it is a well-formed one, else the
    sentinel "REDACTED".

    An ASIN read off an Audible product is not a validated identifier:
    Audible's identifier fields are not reliably identifiers, and a value
    that is not shaped like an ASIN has no business on a log line. Only a
    value that passes the ASIN check is logged.
    """
    return asin if isinstance(asin, str) and is_valid_asin(asin) else "REDACTED"
