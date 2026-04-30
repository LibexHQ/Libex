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

## Before Opening a PR

Every PR must pass both of these locally before pushing:

```bash
ruff check app/ --ignore E501
pytest tests/ -v
```

No ruff warnings. No test failures. CI runs both automatically and will block merge on failure.

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

All response field names use **camelCase** to match AudiMeta's `BookDto` format. This ensures drop-in compatibility for existing consumers.

Examples: `releaseDate`, `lengthMinutes`, `imageUrl`, `whisperSync`, `contentDeliveryType`, `isVvab`, `bookFormat`

Response schemas live in `app/api/routes/<resource>/schemas.py`.

---

## Workflow

```bash
git checkout -b feat/my-feature
# make changes
ruff check app/ --ignore E501
pytest tests/ -v
git add <files>
git commit -m "feat: description"
git push origin feat/my-feature
gh pr create --title "feat: description" --body "..."
```

A maintainer will review and merge your PR. You don't need to do anything after submitting — we'll handle the merge and branch cleanup.

---

## Questions?

Open an issue or comment on an existing one. We're happy to help.