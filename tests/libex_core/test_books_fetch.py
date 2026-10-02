"""
libex_core.audible.books: what fetch_products promises before anything is
sent, the log-value rule on the malformed-author warning, and the static
rules that keep the Audible transport behind one file.

fetch_products is handed `get` rather than owning a client, so every test
here passes a stand-in and asserts on whether it was called. Nothing touches a
network.
"""

# Standard library
import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
import libex_core
from libex_core.audible.books import MAX_ASINS_PER_REQUEST, _parse_authors, fetch_products
from libex_core.exceptions import RegionException

LIBEX_CORE_DIR = Path(libex_core.__file__).resolve().parent
CLIENT_FILE = LIBEX_CORE_DIR / "audible" / "client.py"
BOOKS_LOGGER = "libex_core.audible.books.logger"
MALFORMED_MESSAGE = "Audible sent a malformed author ASIN"


def _asins(count: int) -> list[str]:
    return [f"B0FETCH{i:03d}" for i in range(count)]


# ============================================================
# fetch_products: validated before anything is sent
# ============================================================

def test_fetch_products_takes_get_as_a_required_first_argument():
    """No default transport: the caller decides what reaches Audible."""
    param = inspect.signature(fetch_products).parameters["get"]
    assert param.default is inspect.Parameter.empty
    assert list(inspect.signature(fetch_products).parameters)[0] == "get"


async def test_fetch_products_without_get_is_a_type_error():
    with pytest.raises(TypeError):
        await fetch_products(["B0FETCH001"], "us")


@pytest.mark.parametrize("bad", [
    "B0SHORT",
    "B0ELEVENCHR",
    "B0FETCH00!",
    "../etc/pw",
    "B0FETCH00/",
    "",
    None,
    12345,
])
async def test_fetch_products_rejects_an_invalid_asin_before_any_request(bad):
    get = AsyncMock()
    with pytest.raises(ValueError):
        await fetch_products(get, ["B0FETCH001", bad], "us")
    get.assert_not_awaited()


async def test_fetch_products_error_message_never_echoes_the_rejected_value():
    get = AsyncMock()
    with pytest.raises(ValueError) as exc:
        await fetch_products(get, ["inject\nline"], "us")
    assert "inject" not in str(exc.value)


async def test_fetch_products_rejects_more_than_the_per_request_limit():
    get = AsyncMock()
    with pytest.raises(ValueError):
        await fetch_products(get, _asins(MAX_ASINS_PER_REQUEST + 1), "us")
    get.assert_not_awaited()


async def test_fetch_products_accepts_exactly_the_per_request_limit():
    get = AsyncMock(return_value={"products": [{"asin": "x"}]})
    result = await fetch_products(get, _asins(MAX_ASINS_PER_REQUEST), "us")
    assert result == [{"asin": "x"}]
    get.assert_awaited_once()
    assert get.await_args.args[2]["asins"].count(",") == MAX_ASINS_PER_REQUEST - 1


@pytest.mark.parametrize("region", ["xx", "", "usa", "mars"])
async def test_fetch_products_rejects_an_unknown_region_before_any_request(region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch_products(get, ["B0FETCH001"], region)
    get.assert_not_awaited()


async def test_fetch_products_with_no_asins_makes_no_request():
    get = AsyncMock()
    assert await fetch_products(get, [], "us") == []
    get.assert_not_awaited()


async def test_fetch_products_one_asin_is_a_single_product_request_with_uppercased_asin():
    get = AsyncMock(return_value={"product": {"asin": "B0FETCH001"}})
    result = await fetch_products(get, ["b0fetch001"], " UK ")
    assert result == [{"asin": "B0FETCH001"}]
    region, path, _params = get.await_args.args
    assert region == "uk"
    assert path == "/1.0/catalog/products/B0FETCH001"


async def test_fetch_products_several_asins_are_one_batch_request():
    get = AsyncMock(return_value={"products": [{"asin": "a"}, {"asin": "b"}]})
    result = await fetch_products(get, ["B0FETCH001", "B0FETCH002"], "de")
    assert len(result) == 2
    get.assert_awaited_once()
    region, path, params = get.await_args.args
    assert (region, path) == ("de", "/1.0/catalog/products")
    assert params["asins"] == "B0FETCH001,B0FETCH002"


# ============================================================
# Malformed-author warning: every logged field goes through the log rules
# ============================================================

def _product(author_asin: str, author_name: str, asin: str = "B0DKQBH3CR") -> dict:
    return {"asin": asin, "authors": [{"asin": author_asin, "name": author_name}]}


def _malformed_extra(product: dict) -> dict:
    with patch(BOOKS_LOGGER) as mock_logger:
        _parse_authors(product, "us")
    calls = [c for c in mock_logger.warning.call_args_list if c.args[0] == MALFORMED_MESSAGE]
    assert len(calls) == 1
    return calls[0].kwargs["extra"]


def test_malformed_author_warning_logs_a_safe_value_and_name_as_they_arrived():
    extra = _malformed_extra(_product("Trinka Enell", "Trinka Enell"))
    assert extra["malformed_author_asin"] == "Trinka Enell"
    assert extra["author_name"] == "Trinka Enell"


def test_malformed_author_warning_redacts_a_value_with_control_characters():
    extra = _malformed_extra(_product("bad\x1b[31mvalue", "Real Name"))
    assert extra["malformed_author_asin"] == "REDACTED"
    assert extra["author_name"] == "Real Name"


def test_malformed_author_warning_redacts_an_over_long_value():
    extra = _malformed_extra(_product("v" * 65, "Real Name"))
    assert extra["malformed_author_asin"] == "REDACTED"


def test_malformed_author_warning_redacts_an_unsafe_name_independently():
    extra = _malformed_extra(_product("v", "Jane\nDoe"))
    assert extra["malformed_author_asin"] == "v"
    assert extra["author_name"] == "REDACTED"


def test_malformed_author_warning_redacts_a_name_over_the_length_bound():
    extra = _malformed_extra(_product("v", "n" * 65))
    assert extra["author_name"] == "REDACTED"


def test_malformed_author_warning_logs_the_product_asin_only_if_it_is_an_asin():
    assert _malformed_extra(_product("v", "N", asin="B0DKQBH3CR"))["asin"] == "B0DKQBH3CR"
    assert _malformed_extra(_product("v", "N", asin="not an asin\n"))["asin"] == "REDACTED"
    assert _malformed_extra(_product("v", "N", asin=""))["asin"] == "REDACTED"


# ============================================================
# Static rules over libex_core's own source
# ============================================================

def _core_sources() -> list[Path]:
    return sorted(p for p in LIBEX_CORE_DIR.rglob("*.py"))


def _imports_httpx(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "httpx" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "httpx":
            return True
    return False


def _mentions_libex_client(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "LibexClient":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "LibexClient":
            return True
        if isinstance(node, ast.alias) and node.name == "LibexClient":
            return True
    return False


# httpx belongs to the client module alone. The client is also named by the
# CLI's session module, the one place the command line builds it; every other
# CLI module goes through build_client and never names it.
CLIENT_NAMERS = frozenset({CLIENT_FILE, LIBEX_CORE_DIR / "cli" / "session.py"})


def _stray_uses(path: Path, tree: ast.AST) -> list[str]:
    found = []
    if path != CLIENT_FILE and _imports_httpx(tree):
        found.append("imports httpx")
    if path not in CLIENT_NAMERS and _mentions_libex_client(tree):
        found.append("names LibexClient")
    return found


def test_only_the_client_module_touches_httpx_or_names_libexclient():
    sources = _core_sources()
    assert CLIENT_FILE in sources
    assert len(sources) > 5, "the walk found nothing to check"
    assert all(path in sources for path in CLIENT_NAMERS)
    offenders = {}
    for path in sources:
        found = _stray_uses(path, ast.parse(path.read_text(encoding="utf-8")))
        if found:
            offenders[str(path.relative_to(LIBEX_CORE_DIR))] = found
    assert offenders == {}


def test_the_static_check_would_see_a_stray_httpx_import_or_client():
    """Guards the guard: both predicates fire on the shapes they exist for."""
    assert _imports_httpx(ast.parse("import httpx"))
    assert _imports_httpx(ast.parse("from httpx import AsyncClient"))
    assert not _imports_httpx(ast.parse("import json"))
    assert _mentions_libex_client(ast.parse("c = LibexClient()"))
    assert _mentions_libex_client(ast.parse("import m\nm.LibexClient"))
    assert _mentions_libex_client(ast.parse("from m import LibexClient"))
    assert not _mentions_libex_client(ast.parse("x = 1"))


def test_the_session_module_may_name_the_client_but_not_import_httpx():
    session = LIBEX_CORE_DIR / "cli" / "session.py"
    assert _stray_uses(session, ast.parse("from m import LibexClient")) == []
    assert _stray_uses(session, ast.parse("import httpx")) == ["imports httpx"]


@pytest.mark.parametrize(
    "relative",
    [
        "cli/commands/config.py",
        "cli/commands/completion.py",
        "cli/main.py",
        "cli/environment.py",
        "audible/_retry.py",
        "models.py",
    ],
)
def test_any_other_module_naming_the_client_or_httpx_still_fails(relative):
    path = LIBEX_CORE_DIR / relative
    assert _stray_uses(path, ast.parse("from m import LibexClient")) == ["names LibexClient"]
    assert _stray_uses(path, ast.parse("x = m.LibexClient()")) == ["names LibexClient"]
    assert _stray_uses(path, ast.parse("import httpx")) == ["imports httpx"]


def test_the_client_module_itself_is_unrestricted():
    tree = ast.parse("import httpx\nclass LibexClient: ...\nx = LibexClient()")
    assert _stray_uses(CLIENT_FILE, tree) == []


def _is_path_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id.endswith("_PATH") and node.id.isupper()


def _path_is_built_from_a_module_constant(expr: ast.AST, scope: ast.AST) -> bool:
    if _is_path_constant(expr):
        return True
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        return expr.func.attr == "format" and _is_path_constant(expr.func.value)
    if isinstance(expr, ast.JoinedStr):
        head = expr.values[0] if expr.values else None
        return isinstance(head, ast.FormattedValue) and _is_path_constant(head.value)
    if isinstance(expr, ast.Name):
        assigned = [
            n.value for n in ast.walk(scope)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == expr.id for t in n.targets)
        ]
        return bool(assigned) and all(_path_is_built_from_a_module_constant(a, scope) for a in assigned)
    return False


def _get_calls(tree: ast.AST):
    for func in ast.walk(tree):
        if not isinstance(func, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get":
                yield func, node


def test_every_core_request_path_comes_from_a_module_constant():
    checked = 0
    for name in ("books", "chapters", "series"):
        path = LIBEX_CORE_DIR / "audible" / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func, call in _get_calls(tree):
            checked += 1
            assert len(call.args) >= 2, f"{name}.{func.name}: get() called without a path"
            assert _path_is_built_from_a_module_constant(call.args[1], func), (
                f"{name}.{func.name} line {call.lineno}: request path is not built from a *_PATH constant"
            )
    assert checked >= 5, "the walk found too few get() calls to be checking anything"


def test_the_path_check_rejects_a_literal_and_a_caller_supplied_path():
    def verdict(src: str) -> bool:
        tree = ast.parse(src)
        func, call = next(_get_calls(tree))
        return _path_is_built_from_a_module_constant(call.args[1], func)

    assert verdict("async def f(get, a):\n    await get('us', X_PATH, {})")
    assert verdict("async def f(get, a):\n    p = X_PATH.format(asin=a)\n    await get('us', p, {})")
    assert verdict("async def f(get, a):\n    await get('us', f'{X_PATH}/{a}', {})")
    assert not verdict("async def f(get, a):\n    await get('us', '/1.0/catalog/products', {})")
    assert not verdict("async def f(get, a):\n    await get('us', a, {})")
    assert not verdict("async def f(get, a):\n    p = a\n    await get('us', p, {})")
    assert not verdict("async def f(get, a):\n    await get('us', f'/1.0/{a}', {})")
