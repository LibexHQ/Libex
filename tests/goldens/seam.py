"""
The single place the golden tests reach the code they pin.

Every symbol is imported through one helper here, so moving the fetch and
normalize code to another package means repointing this file and nothing
else. PATCH_* are the consumer-side import paths the persistence tests patch;
they are the second thing a move has to repoint, and they live beside the
helpers for the same reason.
"""

import importlib
from typing import Any


def _books() -> Any:
    return importlib.import_module("libex_core.audible.books")


def _hosted_books() -> Any:
    return importlib.import_module("app.services.audible.books")


def normalize_product(product: dict, region: str) -> dict:
    return _books().normalize_product(product, region)


async def normalize_products(products: list[dict], region: str) -> list[dict]:
    return await _books().normalize_products(products, region)


def normalize_chapters(data: dict, asin: str) -> dict:
    return importlib.import_module("libex_core.audible.chapters").normalize_chapters(data)


def normalize_series(product: dict, region: str) -> dict:
    return importlib.import_module("libex_core.audible.series").normalize_series(product, region)


def settle_flags(book: dict) -> dict:
    return _books().settle_flags(book)


def reproduced_keys() -> frozenset:
    return _books()._REPRODUCED_KEYS


def thread_threshold() -> int:
    return _books().NORMALIZE_THREAD_THRESHOLD


def get_books_by_asins() -> Any:
    return _hosted_books().get_books_by_asins


def search() -> Any:
    return importlib.import_module("app.services.audible.search").search


def get_new_releases() -> Any:
    return importlib.import_module("app.services.audible.releases").get_new_releases


def get_coming_soon() -> Any:
    return importlib.import_module("app.services.audible.releases").get_coming_soon


# Consumer-side patch targets.
PATCH_BOOKS_AUDIBLE_GET = "app.services.audible.books.audible_get"
PATCH_BOOKS_PERSIST = "app.services.audible.books.persist_books_background"
PATCH_BOOKS_CACHE_GET = "app.services.audible.books.cache.get"
PATCH_SEARCH_AUDIBLE_GET = "app.services.audible.search.audible_get"
PATCH_SEARCH_PERSIST = "app.services.audible.search.persist_books_background"
PATCH_RELEASES_AUDIBLE_GET = "app.services.audible.releases.audible_get"
PATCH_RELEASES_PERSIST = "app.services.audible.releases.persist_books_background"
PATCH_RELEASES_CACHE_GET = "app.services.audible.releases.cache.get"
PATCH_RELEASES_CACHE_SET = "app.services.audible.releases.cache.set"
