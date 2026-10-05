## What Changed

<!-- Brief description of what this PR does and why -->

## Checklist

- [ ] `ruff check app/ libex_core/ migrations/ scripts/ tests/ --ignore E501` passes
- [ ] `pytest tests/ -v -m "not integration" --ignore=tests/integration` passes
- [ ] `pytest tests/ -v -m integration` passes (needs Docker and `requirements-dev.lock` installed; all skipped is not a pass)
- [ ] New features include tests
- [ ] Migration revision ID is unique (`ls migrations/versions/`)
- [ ] `down_revision` points to the current latest migration
