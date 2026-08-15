"""Tests for the docs logo: the assets, the OpenAPI key that renders them, and
the README block that publishes them.

ReDoc takes its logo from `info.x-logo` in the OpenAPI document, so the artwork
and the key that points at it are one contract split across two artefacts —
and a third, since the README publishes the same files on the repo's front
page. Each half fails silently on its own: a renamed file still leaves a valid
document, and a wrong URL still returns a served page. These tests join them
back up.
"""

# Standard library
import re
from pathlib import Path

# Third party
import pytest

# Local
# Read through the module rather than binding `app` and `openapi_tags` at
# import time: test_migration_notice reloads app.main to exercise the enabled
# config, which rebinds both. A name bound here at collection would go on
# pointing at the app that reload replaced, and its openapi() delegates to the
# module-level original that the reload has already moved on to.
from app import main as main_module

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"

# The first eight bytes of every PNG. A file of the right name that is not
# actually an image (a placeholder, an unresolved LFS pointer) is served as
# 200 image/png all the same, because StaticFiles types the response from the
# extension and never looks inside.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_LOGO_ASSETS = ["logo.png", "logo-dark.png"]


# ============================================================
# THE ASSETS RESOLVE
# ============================================================


@pytest.mark.parametrize("name", _LOGO_ASSETS)
def test_static_mount_serves_the_committed_logos(client, name):
    """Both logos are committed, so unlike the fetched docs bundles they must
    resolve. This is the check a rename fails before ReDoc renders a broken
    image and the README renders two."""
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == _PNG_MAGIC


# ============================================================
# info.x-logo
# ============================================================


def test_openapi_info_carries_the_logo(client):
    """The exact object ReDoc reads. Asserting the values, not their presence:
    a key holding the wrong URL renders nothing, same as no key at all."""
    spec = client.get("/openapi.json").json()
    assert spec["info"]["x-logo"] == {"url": "/static/logo.png", "altText": "Libex"}


def test_openapi_logo_url_is_served(client):
    """Joins the two halves: whatever URL the document names is fetched, so
    renaming the asset or repointing the key fails here rather than in a
    visitor's browser."""
    url = client.get("/openapi.json").json()["info"]["x-logo"]["url"]
    response = client.get(url)
    assert response.status_code == 200
    assert response.content[:8] == _PNG_MAGIC


# ============================================================
# THE REST OF THE DOCUMENT SURVIVED
# ============================================================


def test_openapi_document_keeps_everything_the_app_was_built_with(client):
    """
    The override adds a key to the document FastAPI already built rather than
    re-deriving it with get_openapi(), precisely so nothing can be dropped by
    an argument left off the restatement. This pins the parts a re-derivation
    silently loses first.

    `tags` is the one with no other guard: lose it and every tag description
    in the docs empties, which reads as a styling change rather than a
    regression. `servers` is asserted here in its default (absent) form for
    the same reason it exists -- a self-hoster's "Try it out" must fire at
    their own host -- and is covered on the enabled path by
    test_migration_notice.test_enabled_openapi_servers_entry_targets_new_host.
    """
    spec = client.get("/openapi.json").json()

    assert spec["tags"] == main_module.openapi_tags
    assert all(tag["description"] for tag in spec["tags"])
    assert "servers" not in spec
    assert spec["info"]["title"] == "Libex"
    assert spec["info"]["version"] == main_module.app.version
    assert spec["info"]["description"]
    assert spec["paths"]


def test_openapi_document_is_built_once_and_cached():
    """The override delegates to FastAPI's own method, which populates
    app.openapi_schema and returns that same dict thereafter. A version that
    re-derives instead would rebuild the whole document on every request to a
    public, unauthenticated endpoint."""
    api = main_module.app
    schema = api.openapi()
    assert api.openapi_schema is schema
    assert api.openapi() is schema


# ============================================================
# THE README PUBLISHES THE SAME FILES
# ============================================================


# Both halves of the <picture> block: srcset for the dark variant, src for the
# fallback. Absolute URLs (the badges) are somebody else's to serve.
_README_LOCAL_IMAGE = re.compile(r'(?:src|srcset)="(?!https?:)([^"]+)"')


def test_readme_image_paths_exist_on_disk():
    """
    Unusual as a test, and it earns its place: the README is rendered by
    GitHub from paths relative to the repo root, so a rename or a missing file
    shows as broken images on the most public page Libex has, and nothing else
    in the gate looks at it. The same cross-artefact check as
    test_middleware.test_docs_pages_load_the_files_the_build_fetches, which
    reads fetch_docs_assets.sh off disk for the same reason.
    """
    referenced = _README_LOCAL_IMAGE.findall(_README.read_text())
    assert referenced, "README references no local images -- has the markup changed?"

    missing = [path for path in referenced if not (_REPO_ROOT / path).is_file()]
    assert missing == [], f"README references images that do not exist: {missing}"
