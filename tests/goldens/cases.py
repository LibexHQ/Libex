"""
Inputs for the golden tests. Outputs live in the JSON files beside this one,
captured from the code before it moved. Inputs are built fresh on every call:
a module-level dict shared across tests is exactly how one test's edit
reaches another.
"""

import copy
from typing import Any

from tests.fixtures.audible_product import AUDIBLE_PRODUCT

REGIONS = ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"]

# Six upstream keys feed the seven settled flags (isAvailable and isBuyable
# are both is_buyable).
FLAG_KEYS = [
    "is_adult_product",
    "is_pdf_url_available",
    "read_along_support",
    "is_listenable",
    "is_buyable",
    "is_vvab",
]


def base() -> dict[str, Any]:
    return copy.deepcopy(AUDIBLE_PRODUCT)


def _nested(depth: int) -> dict:
    node: Any = "leaf"
    for _ in range(depth):
        node = {"n": node}
    return node


def product_cases() -> dict[str, tuple[dict, str]]:
    """name -> (raw product, region)."""
    cases: dict[str, tuple[dict, str]] = {}

    for region in REGIONS:
        cases[f"fixture_{region}"] = (base(), region)

    cases["empty"] = ({}, "us")

    podcast = base()
    podcast.update({
        "asin": "B0PODCAST01",
        "content_type": "Podcast",
        "episode_number": 7,
        "episode_type": "full",
        "relationships": [
            {"asin": "B0SERIES01", "relationship_type": "series", "sequence": "1", "title": "A Series"},
            {"asin": "B0EP00001", "relationship_type": "episode", "sort": "1", "url": "/pd/ep1"},
            {"asin": "B0EP00002", "relationship_type": "episode", "sort": "2", "url": "/pd/ep2"},
            {"asin": "B0SEASON01", "relationship_type": "season", "sort": "1"},
        ],
    })
    cases["podcast_episodes"] = (podcast, "us")

    not_podcast_episode = base()
    not_podcast_episode.update({"episode_number": 7, "episode_type": "full"})
    cases["non_podcast_with_episode_fields"] = (not_podcast_episode, "us")

    no_plans = base()
    del no_plans["plans"]
    cases["plans_absent"] = (no_plans, "us")
    cases["plans_empty"] = ({**base(), "plans": []}, "us")
    cases["plans_unreadable"] = ({**base(), "plans": [{"plan_id": 1}, {"plan_name": None}]}, "us")
    cases["plans_partial"] = ({**base(), "plans": [{"plan_id": 1}, {"plan_name": "US Minerva"}]}, "us")
    cases["plans_present"] = ({**base(), "plans": [{"plan_name": "A"}, {"plan_name": "B"}]}, "us")

    for key in FLAG_KEYS:
        absent = base()
        del absent[key]
        cases[f"flag_{key}_absent"] = (absent, "us")
        cases[f"flag_{key}_false"] = ({**base(), key: False}, "us")
        cases[f"flag_{key}_true"] = ({**base(), key: True}, "us")

    cases["extras_nul_char"] = (
        {**base(), "keyword\x00x": "a\x00b", "nested": {"k": ["x\x00", "y"]}}, "us")
    cases["extras_inf"] = ({**base(), "weird": float("inf"), "neg": float("-inf"), "nan": float("nan")}, "us")
    cases["extras_oversized_int"] = ({**base(), "big": 10**5000, "ok": 2**40, "flag": True}, "us")
    cases["extras_depth_33"] = ({**base(), "deep": _nested(32)}, "us")
    cases["extras_depth_32"] = ({**base(), "deep": _nested(31)}, "us")
    cases["extras_blob_over_64k"] = ({**base(), "blob": "x" * 70000}, "us")

    # 40 KB as UTF-8 but 120 KB were it escaped to ASCII: pins that the size
    # cap measures the encoding the blob is actually stored in.
    cases["extras_unicode_under_cap"] = ({**base(), "title": "Caf\u00e9 \u65e5\u672c", "blob": "\u00e9" * 20000}, "jp")

    cases["narrators_edge"] = ({
        **base(),
        "narrators": [
            {"name": "   "},
            {"name": "\tTab Padded\t"},
            {"asin": "NONAME"},
            {"name": ""},
            {"name": "Plain Name"},
        ],
    }, "us")

    cases["authors_edge"] = ({
        **base(),
        "authors": [
            {"asin": "Trinka Enell", "name": "Trinka Enell"},
            {"asin": "ABCDEFGHIJKLMNOPQ", "name": "Too Long"},
            {"asin": "v", "name": "Stray"},
            {"asin": "B0AUTHOR01", "name": "Good"},
            {"name": "No Asin"},
        ],
    }, "us")

    return cases


def thread_batch(count: int) -> list[dict]:
    """count distinct products, deterministic, mixing shapes."""
    out = []
    for i in range(count):
        p = base()
        p["asin"] = f"B0BATCH{i:04d}"
        p["title"] = f"Batch Title {i}"
        if i % 3 == 0:
            del p["plans"]
        if i % 4 == 0:
            p["is_vvab"] = False
        if i % 5 == 0:
            p["extra_key"] = {"i": i}
        out.append(p)
    return out


def chapter_cases() -> dict[str, tuple[dict, str]]:
    full = {
        "content_metadata": {
            "chapter_info": {
                "brandIntroDurationMs": 2043,
                "brandOutroDurationMs": 5062,
                "is_accurate": True,
                "runtime_length_ms": 36000000,
                "runtime_length_sec": 36000,
                "chapters": [
                    {"length_ms": 1000, "start_offset_ms": 0, "start_offset_sec": 0, "title": "Opening Credits"},
                    {"length_ms": 2500, "start_offset_ms": 1000, "start_offset_sec": 1, "title": "Chapter 1"},
                ],
            }
        }
    }
    brandless = copy.deepcopy(full)
    del brandless["content_metadata"]["chapter_info"]["brandIntroDurationMs"]
    del brandless["content_metadata"]["chapter_info"]["brandOutroDurationMs"]
    nested = copy.deepcopy(full)
    nested["content_metadata"]["chapter_info"]["chapters"] = [
        {
            "length_ms": 9000, "start_offset_ms": 0, "start_offset_sec": 0, "title": "Part One",
            "chapters": [
                {"length_ms": 4000, "start_offset_ms": 0, "start_offset_sec": 0, "title": "Sub A"},
                {"length_ms": 5000, "start_offset_ms": 4000, "start_offset_sec": 4, "title": "Sub B"},
            ],
        },
        {"length_ms": 100, "title": "Sparse"},
    ]
    return {
        "full": (full, "B0CHAPTER01"),
        "empty": ({}, "B0CHAPTER01"),
        "brand_keys_missing": (brandless, "B0CHAPTER01"),
        "nested_subchapters": (nested, "B0CHAPTER01"),
    }


def series_cases() -> dict[str, tuple[dict, str]]:
    return {
        "full_us": (
            {"asin": "B0SERIES01", "title": "A Series", "publisher_summary": "<p>Sum &amp; more.</p>"},
            "us",
        ),
        "empty_jp": ({}, "jp"),
    }


def settle_cases() -> dict[str, dict]:
    """Dicts as _normalize_product produces them, with Nones in place."""
    all_none = {
        "asin": "B0SETTLE01", "plans": None, "isListenable": None, "isAvailable": None,
        "isBuyable": None, "isVvab": None, "explicit": None, "hasPdf": None, "whisperSync": None,
    }
    mixed = {**all_none, "isListenable": True, "isVvab": False, "plans": ["A"]}
    foreign = {"asin": "B0SETTLE02", "isVvab": None}  # keys never carried are never added
    return {"all_none": all_none, "mixed": mixed, "foreign_subset": foreign, "empty": {}}
