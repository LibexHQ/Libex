#!/usr/bin/env sh
# Fetches the Swagger UI and ReDoc assets that /docs and /redoc are served
# from, verifying each against a pinned checksum.
#
# Libex serves these itself rather than letting the browser fetch them from a
# CDN. FastAPI's defaults point at cdn.jsdelivr.net and fastapi.tiangolo.com,
# and ReDoc's template additionally pulls a stylesheet from fonts.googleapis.com
# -- so opening the docs in a browser sent the visitor's real IP address to
# three third parties. Libex records nothing that identifies a caller, and that
# claim has to hold for the pages it serves, not only for the lines it logs.
#
# Versions are pinned exactly. FastAPI's defaults track floating major tags
# (swagger-ui-dist@5, redoc@2), which means a browser executes whatever the CDN
# resolves those to on the day. Pinning plus a checksum makes the bytes we
# serve reproducible and makes a substituted file a build failure rather than
# something a visitor silently runs.
#
# Run from the repo root. The Dockerfile runs this at build time; run it by
# hand once if you want /docs to render correctly against a local checkout.

set -eu

SWAGGER_VERSION="5.32.13"
REDOC_VERSION="2.5.3"

DEST="app/static/docs"

# sha256, verified at fetch time. Update alongside a version bump, never
# separately -- a version change with a stale checksum should fail loudly.
SWAGGER_JS_SHA="5f3be5d9cf40cdd60dca0dafeaf8743fd858d1b3bb717bbdaebf7201303f63d7"
SWAGGER_CSS_SHA="9e617d9ac0afb0e430c11a17366de8624db7ce34c99ebd297443f0048ce30899"
REDOC_JS_SHA="1320f442151c57c447d3b70c7ffc6c4f86d08464020fe34c8cc5d3164e9944f0"

mkdir -p "$DEST"

fetch() {
    url="$1"
    out="$2"
    want="$3"

    curl -sfL "$url" -o "$out"

    got=$(sha256sum "$out" | cut -d' ' -f1)
    if [ "$got" != "$want" ]; then
        echo "checksum mismatch for $out" >&2
        echo "  expected $want" >&2
        echo "  got      $got" >&2
        rm -f "$out"
        exit 1
    fi
    echo "ok  $out"
}

fetch "https://cdn.jsdelivr.net/npm/swagger-ui-dist@${SWAGGER_VERSION}/swagger-ui-bundle.js" \
      "$DEST/swagger-ui-bundle.js" "$SWAGGER_JS_SHA"

fetch "https://cdn.jsdelivr.net/npm/swagger-ui-dist@${SWAGGER_VERSION}/swagger-ui.css" \
      "$DEST/swagger-ui.css" "$SWAGGER_CSS_SHA"

fetch "https://cdn.jsdelivr.net/npm/redoc@${REDOC_VERSION}/bundles/redoc.standalone.js" \
      "$DEST/redoc.standalone.js" "$REDOC_JS_SHA"
