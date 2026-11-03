"""
After everything else: nothing a caller sent, and no credential, is in the
app's log files or the container logs. run.sh greps again from outside.
"""

# Standard library
import hashlib
import os


def test_privacy_inputs_reach_no_log(sbx):
    canary = sbx.canary()
    spoof = {
        "X-Forwarded-For": "203.0.113.77", "CF-Connecting-IP": "203.0.113.77",
        "X-Real-IP": "203.0.113.77", "Cookie": f"sbx={canary}", "Referer": f"https://example.com/{canary}",
    }
    extra = {canary: canary, "q": canary}
    sbx.http.get("/db/delisted", params={"region": "us", **extra}, headers=spoof)
    assert sbx.post(sbx.book(), params=extra, headers=spoof).status_code == 200
    assert sbx.post(sbx.book(), params=extra, headers=spoof, auth="wrong", audible=False).status_code == 401
    assert sbx.post(sbx.book(), region="zz", params=extra, headers=spoof, audible=False).status_code == 400

    app = sbx.app_logs()
    everything = app + sbx.container_logs()
    # Positive controls first: a grep over logs that lack these proves nothing.
    assert sbx.ua in app
    assert "Book entry complete" in app
    assert "request_id" in app

    ingest, seed = os.environ["LIBEX_SANDBOX_TOKEN"], os.environ["LIBEX_SANDBOX_SEED_TOKEN"]
    needles = {
        "ingest token": ingest, "seed token": seed,
        "ingest digest": hashlib.sha256(ingest.encode()).hexdigest(),
        "seed digest": hashlib.sha256(seed.encode()).hexdigest(),
        "bearer": "Bearer", "test-net address": "203.0.113.77",
        **{f"canary {i}": c for i, c in enumerate(sbx.canaries)},
    }
    leaks = [label for label, needle in needles.items() if needle in everything]
    assert leaks == []
