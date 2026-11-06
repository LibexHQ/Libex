"""
The text of public.libex_fold and the folded indexes exists in four places that
have to agree: the hosted revision, the core revision, the models and the
golden. An existing function is compared with the revision's body character for
character, so a copy that drifts turns a clean upgrade into a refusal.
"""

# Standard library
import importlib.util
import json
from pathlib import Path

# Third party
import pytest

# Local
from libex_core.storage import models

ROOT = Path(__file__).resolve().parent.parent.parent
GOLDEN = json.loads((Path(__file__).resolve().parent / "golden_postgres_ddl.json").read_text())


def _load(directory: Path, prefix: str):
    path = next(directory.glob(f"{prefix}_*.py"))
    spec = importlib.util.spec_from_file_location(f"revision_{prefix}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOSTED = _load(ROOT / "migrations" / "versions", "e6a1d4f93b27")
CORE = _load(ROOT / "libex_core" / "storage" / "migrations" / "versions", "c8d2a5e71f39")


def _golden_lines():
    return {line for ddl in GOLDEN.values() for line in ddl.split("\n")}


def _folded_lines(golden_lines):
    return {line for line in golden_lines if line.startswith("CREATE INDEX") and any(
        f" {name} " in line for name in models.FOLDED_INDEX_NAMES
    )}


def test_the_function_body_is_the_same_in_all_four_places():
    copies = {
        "hosted": HOSTED.FOLD_FUNCTION_BODY,
        "core": CORE.FOLD_FUNCTION_BODY,
        "models": models.FOLD_FUNCTION_BODY,
        "golden": GOLDEN["libex_fold"].split("$$")[1],
    }
    assert len(set(copies.values())) == 1, copies


def test_the_function_ddl_is_the_same_in_all_four_places():
    copies = {
        "hosted": HOSTED.FOLD_FUNCTION_DDL,
        "core": CORE.FOLD_FUNCTION_DDL,
        "models": models.FOLD_FUNCTION_DDL,
        "golden": GOLDEN["libex_fold"],
    }
    assert len(set(copies.values())) == 1, copies


def test_a_drifted_copy_would_not_pass_the_identity_check():
    drifted = models.FOLD_FUNCTION_BODY.replace("pg_catalog.lower", "pg_catalog.upper")
    assert drifted != GOLDEN["libex_fold"].split("$$")[1]
    assert drifted != HOSTED.FOLD_FUNCTION_BODY and drifted != CORE.FOLD_FUNCTION_BODY


def test_the_function_is_schema_qualified_and_has_no_search_path_dependence():
    body = models.FOLD_FUNCTION_BODY
    assert "pg_catalog.lower(" in body
    assert "public.unaccent('public.unaccent'::regdictionary," in body
    assert " SET " not in models.FOLD_FUNCTION_DDL
    for volatility in ("IMMUTABLE", "STRICT", "PARALLEL SAFE"):
        assert volatility in models.FOLD_FUNCTION_DDL
    assert "CREATE OR REPLACE" not in models.FOLD_FUNCTION_DDL


def test_both_chains_build_the_same_indexes():
    assert HOSTED.INDEXES == CORE.INDEXES
    assert {name for name, *_ in HOSTED.INDEXES} == models.FOLDED_INDEX_NAMES
    assert HOSTED.ANALYZED_TABLES == CORE.ANALYZED_TABLES
    assert HOSTED.EXTENSION_DDL == CORE.EXTENSION_DDL
    assert HOSTED.UNACCENT_IS_THE_EXTENSION_SQL == CORE.UNACCENT_IS_THE_EXTENSION_SQL


def test_the_migration_index_sql_is_exactly_the_golden_lines():
    migrated = {f"CREATE INDEX {name} ON {table} {tail}" for name, table, tail, _ in CORE.INDEXES}
    golden = _folded_lines(_golden_lines())
    assert migrated == golden
    assert len(golden) == len(models.FOLDED_INDEX_NAMES)


def test_a_changed_index_would_not_match_the_golden():
    """A bound separator, a missing schema or a changed operator class all
    change the text; none of them is a golden line."""
    golden = _golden_lines()
    name, table, tail, _ = CORE.INDEXES[0]
    line = f"CREATE INDEX {name} ON {table} {tail}"
    assert line in golden
    for changed in (
        line.replace("' '", "$1"),
        line.replace("public.libex_fold", "libex_fold"),
        line.replace("public.gin_trgm_ops", "public.gist_trgm_ops"),
    ):
        assert changed != line and changed not in golden


@pytest.mark.parametrize("module", [HOSTED, CORE], ids=["hosted", "core"])
def test_each_expected_definition_names_its_own_index_and_table(module):
    for name, table, _tail, expected in module.INDEXES:
        assert expected.startswith(f"CREATE INDEX {name} ON {table} ")
        assert "public." not in expected


def test_the_chain_heads_follow_the_previous_heads():
    assert HOSTED.down_revision == "a8c3e51f7d29"
    assert CORE.down_revision == "b5e9c3a7f184"
    assert HOSTED.revision == "e6a1d4f93b27" and CORE.revision == "c8d2a5e71f39"
