# Contributing to Libex

Thanks for your interest in contributing. This document covers the conventions and requirements for getting a PR merged.

---

## Branch Naming

All work happens on feature/fix branches off `main`. Branch protection is enabled — direct commits to `main` are not allowed.

| Prefix | Use |
|--------|-----|
| `feat/` | New features or endpoints |
| `fix/` | Bug fixes |
| `docs/` | Documentation changes |
| `refactor/` | Code restructuring with no behavior change |

Examples: `feat/add-narrator-search`, `fix/author-null-asin-race`, `docs/update-readme`

---

## Commit Messages

Use imperative mood with a type prefix:

```
feat: Add narrator search endpoint
fix: Handle null asin in author upsert
docs: Update README with new db endpoints
refactor: Extract book normalization into helper
```

---

## Setup

Install dependencies before running anything below — this is the same lock CI installs from, and it's what pulls in the test runners:

```bash
pip install --require-hashes -r requirements-dev.lock
```

---

## Before Opening a PR

Every PR must pass all of these locally before pushing:

```bash
ruff check app/ libex_core/ migrations/ scripts/ tests/ --ignore E501
pytest tests/ -v -m "not integration" --ignore=tests/integration  # matches the `backend` job's unit tests
pytest tests/ -v -m integration  # matches the `integration` job (needs Docker and `requirements-dev.lock` installed; all skipped is not a pass)
```

No ruff warnings. No test failures. New code needs new tests, and the full suite must stay green with no regressions to existing tests.

If you touch `pyproject.toml`, `libex_core/` or `libex-core-data/`, also check that the package builds. CI borrows `build`, `flit_core` and `pyproject_hooks` from a hash-checked tooling venv, and `tests/libex_core/test_packaging.py` skips itself when they are missing, so a local skip is not a pass:

```bash
python3 -m venv <dir>
<dir>/bin/pip install --require-hashes -r requirements-tooling.lock
<dir>/bin/python -m build --no-isolation --outdir <outdir> .
```

CI runs every check above, plus the packaging check and a dependency audit, and will block merge on failure.

---

## Migrations

If your change requires a database migration:

1. Check existing revision IDs: `ls migrations/versions/`
2. Your `revision` must be a **unique** 12-character hex string — do not reuse an existing one
3. `down_revision` must point to the **current latest** migration
4. Test the migration locally before pushing

Duplicate revision IDs break Alembic's chain and will be rejected.

---

## Tests

New features require tests. When writing tests:

- **Mock at the router's import location**, not the service module. For example:
  ```python
  # Correct
  patch("app.api.routes.books.router.get_book_by_asin")

  # Wrong
  patch("app.services.audible.books.get_book_by_asin")
  ```
- **Error assertions** use `response.json()["error"]` — not `response.json()["detail"]`
- Match the patterns in existing test files. Review `tests/` before writing new tests

---

## Response Schemas

All response field names use **camelCase** because the response shapes derive from AudiMeta's `BookDto` format. Changes to them are additive only: never remove a field or alter a shape, because callers can't be warned.

Examples: `releaseDate`, `lengthMinutes`, `imageUrl`, `whisperSync`, `contentDeliveryType`, `isVvab`, `bookFormat`

The response models (`BookResponse`, `BulkBookResponse`, `ChapterResponse`, `SeriesResponse`, `AuthorResponse`, `NarratorProfileResponse` and the objects nested in them), and the Audiobookshelf search response (`AbsSearchResponse`), live in `libex_core/models.py`. What stays in `app/api/routes/<resource>/schemas.py` is the search query parameters.

---

## Workflow

```bash
git checkout -b feat/my-feature
# make changes
ruff check app/ libex_core/ migrations/ scripts/ tests/ --ignore E501
pytest tests/ -v -m "not integration" --ignore=tests/integration  # matches the `backend` job's unit tests
pytest tests/ -v -m integration  # matches the `integration` job; requires Docker
git add <files>
git commit -m "feat: description"
git push origin feat/my-feature
gh pr create --title "feat: description" --body "..."
```

A maintainer will review and merge your PR. You don't need to do anything after submitting — we'll handle the merge and branch cleanup.

---

## Questions?

Open an issue or comment on an existing one. We're happy to help.