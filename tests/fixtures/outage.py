"""
The exact wire shape of an Audible outage, asserted in one place.

A 503 with Retry-After and Cache-Control: no-store, and a body that adds
`retryAfter` to the usual {error, status_code, code}. Every route test that
mocks an outage asserts all of it through this helper, so a route cannot pass
by getting only the status right.
"""

RETRY_AFTER_SECONDS = 30


def assert_outage_503(response, message):
    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "error": message,
        "status_code": 503,
        "code": "upstream_unavailable",
        "retryAfter": RETRY_AFTER_SECONDS,
    }
