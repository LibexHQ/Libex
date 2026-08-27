"""
Regression tests for the /redoc HTML-escaping fix.

`app/main.py`'s redoc() route interpolates three values into an inline
<script> block: `app.openapi_url`, `_REDOC_THEME`, and `app.version`. Plain
`json.dumps()` guarantees valid JSON but leaves `<`, `>` and `&` untouched, so
a value containing `</script>` closes the tag at the HTML-parser level no
matter how the JSON inside it is quoted -- text after that point is parsed as
markup, not JS, regardless of what the JSON payload actually says. `app.version`
is the operator-controlled one in practice (read from the APP_VERSION
environment variable via Settings), but the theme dict and the OpenAPI URL get
the same treatment rather than trusting today's callers to stay safe, and each
of the three is a separate call site that can regress independently.

`_html_safe_json()` closes this by escaping those three characters to their
\\uXXXX forms after `json.dumps()`. These tests exist because reverting any one
of the three call sites back to plain `json.dumps()` currently passes the
whole suite -- nothing here proves any of them stay escaped.

Scanning the rendered page for a literal `</script>` rather than asserting the
escaped form appears: the escaped form appearing is not proof the raw form is
absent, since the fix could add the safe copy alongside an unescaped one and
still pass that weaker check.
"""

# Standard library
import re

# Third party
from fastapi.testclient import TestClient

# Local
from app import main as main_module

# A payload that, if not escaped, closes the surrounding <script> block and
# opens a new one -- the exact shape of the fix's own threat model, not just
# a bare `<` or `>`.
_HOSTILE = 'x</script><script>alert(document.domain)</script>'

# The redoc() template's own two legitimate closing tags: the loader
# (`<script src="...redoc.standalone.js"></script>`) and the inline
# Redoc.init block. Pinned by test_redoc_page_has_exactly_two_closing_tags
# below so a template change that adds a real third one doesn't make every
# hostile-payload test pass for the wrong reason.
_LEGITIMATE_CLOSING_TAGS = 2

_CLOSING_SCRIPT_TAG = re.compile(r"</\s*script\s*>", re.IGNORECASE)


def _redoc_html(app) -> str:
    return TestClient(app).get("/redoc").text


def test_redoc_page_has_exactly_two_closing_tags():
    """Baseline the hostile-payload tests below compare against. If this
    number ever changes, it means the template grew or lost a real <script>
    tag, and the tests below need to move with it rather than silently
    tolerating an extra closing tag as if it were injected."""
    html = _redoc_html(main_module.app)
    assert len(_CLOSING_SCRIPT_TAG.findall(html)) == _LEGITIMATE_CLOSING_TAGS


def test_hostile_openapi_url_cannot_close_the_script_tag(monkeypatch):
    """Covers the first of the three call sites: `_html_safe_json(app.openapi_url)`."""
    monkeypatch.setattr(main_module.app, "openapi_url", f"/openapi.json{_HOSTILE}")
    html = _redoc_html(main_module.app)
    assert len(_CLOSING_SCRIPT_TAG.findall(html)) == _LEGITIMATE_CLOSING_TAGS


def test_hostile_theme_value_cannot_close_the_script_tag(monkeypatch):
    """Covers the second call site: `_html_safe_json(_REDOC_THEME)`. Patched
    three levels deep (theme.colors.primary.main) rather than replacing the
    whole dict, so this also proves the escaping is applied to the serialized
    JSON as a whole and not just to a value some earlier code path already
    sanitized at the top level."""
    monkeypatch.setitem(
        main_module._REDOC_THEME["theme"]["colors"]["primary"], "main", _HOSTILE
    )
    html = _redoc_html(main_module.app)
    assert len(_CLOSING_SCRIPT_TAG.findall(html)) == _LEGITIMATE_CLOSING_TAGS


def test_hostile_app_version_cannot_close_the_script_tag(monkeypatch):
    """Covers the third call site: `_html_safe_json(app.version)`. app.version
    is set from the APP_VERSION environment variable at startup, so unlike the
    other two this one is operator-controlled input in production, not a
    value Libex's own code constructs."""
    monkeypatch.setattr(main_module.app, "version", _HOSTILE)
    html = _redoc_html(main_module.app)
    assert len(_CLOSING_SCRIPT_TAG.findall(html)) == _LEGITIMATE_CLOSING_TAGS


# ============================================================
# THE ensure_ascii DEPENDENCY
# ============================================================


def test_html_safe_json_neutralises_js_line_terminators():
    """
    U+2028 (LINE SEPARATOR) and U+2029 (PARAGRAPH SEPARATOR) are valid inside
    a JSON string but are JS statement terminators when they land raw inside
    a <script> block, independent of quoting -- a string containing one of
    them, naively embedded, breaks the surrounding JS rather than escaping
    out of it.

    Nothing in `_html_safe_json`'s own three `.replace()` calls touches
    either character -- only `<`, `>` and `&` are handled explicitly. What
    actually neutralises them is `json.dumps()`'s `ensure_ascii=True`
    default, which \\u-escapes every non-ASCII character before the replaces
    ever run. That default is never set explicitly in `_html_safe_json` and
    is not stated anywhere near it, so this pins the behavior it currently
    provides for free: if a future edit adds `ensure_ascii=False` (say, to
    shrink the payload), this fails instead of the regression waiting to be
    found by a browser choking on a raw separator in production.
    """
    encoded = main_module._html_safe_json("line break end")
    assert " " not in encoded
    assert " " not in encoded
    assert "\\u2028" in encoded
    assert "\\u2029" in encoded
