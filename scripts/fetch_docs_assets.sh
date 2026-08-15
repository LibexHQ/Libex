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
#
# Bumping a version is a four-step ritual, in this order:
#   1. Change the version and leave the old sha. The run must fail on the
#      checksum -- a pin that cannot be observed failing is not known to be
#      live.
#   2. Re-derive the new sha from the artifact as published, not from the copy
#      just downloaded here; a hash taken from the same bytes that would be
#      served proves only that the download was internally consistent.
#   3. Re-derive REDOC_LOGO_EXPECTED by inspecting the new bundle, and
#      re-enumerate the hosts referenced by all three assets rather than that
#      one -- any of the three can gain a call in a release the previous one
#      did not make. Only hosts sitting where the browser acts on them by
#      itself count: src=, url(), @import, a protocol-relative //host, new
#      Image. Nearly all the hosts in these files are somewhere inert instead
#      -- json-schema.org as $id/$schema, github and stackoverflow in error
#      text and comments, w3.org as an SVG xmlns inside a data: URI -- and of
#      the nine redoc 2.5.3 names, one is actually fetched: the logo below.
#      That ratio is why this is read by hand and not asserted. A check that
#      flagged eight harmless hosts at every bump would be waved through, and
#      a waved-through check is worse than none because it still reads as
#      coverage. Swagger UI is the same shape: it names validator.swagger.io
#      as the default for a badge it never mounts, closed by construction with
#      validatorUrl null where /docs is served rather than patched out here.
#   4. Only then update the count constant. Note the count is taken with
#      grep -F, a fixed string, while the substitution below is a BRE where .
#      is a wildcard -- so the two do not match identically. Harmless in this
#      direction: the BRE can only match a superset of the literal, and the
#      host check after the substitution fails the build on anything left.

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

# The bundle asks the visitor's browser for this logo as it renders, from a
# sidebar footer that is mounted unconditionally -- so serving the file
# unpatched would hand Redocly the IP address of everyone who opens /redoc,
# which is the whole thing these self-hosted assets exist to prevent. A
# transparent pixel inlined as a data URI resolves without a request. The
# logo is Redocly's trademark and is absent from the npm package, so it is
# neutralised rather than vendored; the enclosing link and its "API docs by
# Redocly" text are untouched, so the attribution still stands.
REDOC_LOGO_URL="https://cdn.redoc.ly/redoc/logo-mini.svg"
# Occurrences in redoc 2.5.3, counted when this patch was written. A
# version bump must re-derive this rather than assume it.
REDOC_LOGO_EXPECTED=1
BLANK_PIXEL="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

# The bundle is minified onto a handful of very long lines, so grep -c would
# report 1 no matter how many times the string appears. Counting matches with
# -o is what makes the assertion mean anything. grep exits 1 when it matches
# nothing, but a pipeline reports only wc's status, so a count of 0 reaches
# the comparison below and produces a readable failure instead of set -e
# killing the script silently.
occurrences() {
    grep -F -o -- "$2" "$1" | wc -l | tr -d '[:space:]'
}

found=$(occurrences "$DEST/redoc.standalone.js" "$REDOC_LOGO_URL")
if [ "$found" != "$REDOC_LOGO_EXPECTED" ]; then
    echo "unexpected logo reference count in $DEST/redoc.standalone.js" >&2
    echo "  expected $REDOC_LOGO_EXPECTED" >&2
    echo "  found    $found" >&2
    rm -f "$DEST/redoc.standalone.js"
    exit 1
fi

# | as the delimiter: the URL and the base64 replacement both contain /, and
# neither contains |. Written to a temporary file and moved into place rather
# than edited with sed -i, which is not portable.
sed "s|${REDOC_LOGO_URL}|${BLANK_PIXEL}|g" "$DEST/redoc.standalone.js" > "$DEST/redoc.standalone.js.tmp"
mv "$DEST/redoc.standalone.js.tmp" "$DEST/redoc.standalone.js"

# Asserted against the host rather than the URL just replaced, so a second
# reference introduced by a future release is caught instead of slipping
# through a check that only ever looked for the one already known about.
remaining=$(occurrences "$DEST/redoc.standalone.js" "cdn.redoc.ly")
if [ "$remaining" != "0" ]; then
    echo "cdn.redoc.ly still referenced in $DEST/redoc.standalone.js" >&2
    echo "  expected 0" >&2
    echo "  found    $remaining" >&2
    rm -f "$DEST/redoc.standalone.js"
    exit 1
fi
echo "ok  $DEST/redoc.standalone.js logo neutralised"
