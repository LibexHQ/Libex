"""The series-index revision bounds its lock wait before it builds anything."""

# Standard library
import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
PATH = next((ROOT / "migrations" / "versions").glob("5b1e7d3a9c42_*.py"))


def _load():
    spec = importlib.util.spec_from_file_location("series_index_revision", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_sets_a_transaction_scoped_lock_timeout_before_the_index():
    module = _load()
    calls = []
    with patch.object(module, "op") as op:
        op.execute.side_effect = lambda sql: calls.append(("execute", str(sql)))
        op.create_index.side_effect = lambda *a, **k: calls.append(("create_index", a))
        module.upgrade()

    assert calls[0] == ("execute", "SET LOCAL lock_timeout = '5s'")
    assert calls[1][0] == "create_index"
    assert calls[1][1][:3] == (
        "book_series_series_index", "book_series", ["series_asin", "series_region"]
    )


def test_the_credit_order_revision_adds_a_nullable_position_without_a_default():
    path = next((ROOT / "migrations" / "versions").glob("3c8e5a1f7d94_*.py"))
    spec = importlib.util.spec_from_file_location("credit_order_revision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    with patch.object(module, "op") as op:
        op.execute.side_effect = lambda sql: calls.append(str(sql))
        module.upgrade()
        module.downgrade()

    assert calls[0] == "SET LOCAL lock_timeout = '5s'"
    adds = [c for c in calls if "ADD COLUMN" in c]
    assert len(adds) == 3 and all("INTEGER" in c and "DEFAULT" not in c and "NOT NULL" not in c for c in adds)
    assert len([c for c in calls if "DROP COLUMN" in c]) == 3
    assert module.down_revision == "5b1e7d3a9c42"


def test_the_manual_entry_revision_sets_the_lock_timeout_then_adds_a_nullable_column_without_a_default():
    path = next((ROOT / "migrations" / "versions").glob("b7e2d94c1a68_*.py"))
    spec = importlib.util.spec_from_file_location("manual_entry_revision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    up, down = [], []
    with patch.object(module, "op") as op:
        op.execute.side_effect = lambda sql: up.append(str(sql))
        module.upgrade()
        op.execute.side_effect = lambda sql: down.append(str(sql))
        module.downgrade()

    assert up[0] == "SET LOCAL lock_timeout = '5s'"
    adds = [c for c in up if "ADD COLUMN" in c]
    assert len(adds) == 1 and "manual_entry_at" in adds[0]
    assert "NOT NULL" not in adds[0] and "DEFAULT" not in adds[0]
    assert len(down) == 1 and "DROP COLUMN" in down[0] and "manual_entry_at" in down[0]
    assert module.down_revision == "3c8e5a1f7d94"
