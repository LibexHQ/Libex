"""
Region-pollution repair: the parts that decide whether a run is safe.

The script replaces stored rows with Audible's answer, so what these pin is
everything that stands between a row and an unwanted delete: the frozen list,
the dry run, an answer that is not a product, the backup line, the cursor, the
identity values put back on a rewritten row, the stop, the pacing floor and
the dedicated-exit guard. The transaction itself, against a real database,
is covered in tests/integration/test_repair_region_pollution.py; here it is
replaced, because a fake session would only restate its own script.
"""

# Standard library
import argparse
import asyncio
import importlib.util
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.db.models import Book
from libex_core.exceptions import AudibleAPIException, NotFoundException
from tests.fixtures.audible_product import AUDIBLE_PRODUCT
from tests.scripts.conftest import set_hosted_transport
import scripts.repair_region_pollution as repair
from scripts.repair_region_pollution import (
    BackupFile,
    RepairList,
    _Audible,
    _backup_record,
    _decode_row,
    _encode_row,
    _fetch_batch,
    _fetch_track,
    _identity_values,
    _next_batch,
    _Run,
    _ThrottleSentinel,
    _check_abort,
    _verify_dedicated_proxy,
    check_pair_unrepaired,
    check_resume,
    process,
    read_backup,
)

ASIN = "0123456789"


def _list(*pairs, series_authors=()):
    return RepairList(list(pairs), list(series_authors), {}, "2026-10-03T00:00:00+00:00")


def _snapshot_of(primary=True):
    book = {
        "asin": ASIN, "region": "us", "title": "Old", "is_primary": primary,
        "created_at": "2026-01-02T03:04:05.123456+00:00", "plans": None,
        "audible_extras": {"a": 1}, "description": "from de",
    }
    return {
        "book": book,
        "links": {
            "author_book": [{"author_id": 1, "book_asin": ASIN, "book_region": "us"},
                            {"author_id": 2, "book_asin": ASIN, "book_region": "us"}],
            "book_narrator": [{"narrator_name": "N", "book_asin": ASIN, "book_region": "us"}],
            "book_series": [{"book_asin": ASIN, "book_region": "us", "series_asin": "B0S",
                             "series_region": "de", "position": "2"}],
            "book_genre": [{"book_asin": ASIN, "book_region": "us", "genre_asin": "G1"}],
        },
        "track": {"asin": ASIN, "region": "us", "chapters": {"chapters": [{"title": "One"}]},
                  "created_at": "2026-01-02T03:04:05+00:00", "updated_at": "2026-01-02T03:04:05+00:00"},
        "foreign": {"author_ids": [2], "series": [["B0S", "de"]]},
    }


def _factory():
    """A session factory whose sessions are inert: every test here that gets
    one has patched out whatever would have used it."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session)


class FakeAudible:
    """Stands in for _Audible: canned answers, and a record of every request."""

    def __init__(self, products=None, chapters=None, product_error=None, chapters_error=None):
        self._products = products or []
        self._chapters = chapters
        self._product_error = product_error
        self._chapters_error = chapters_error
        self.requests = []

    async def products(self, asins, region):
        self.requests.append(("products", tuple(asins), region))
        if self._product_error:
            raise self._product_error
        return self._products

    async def chapters(self, asin, region):
        self.requests.append(("chapters", asin, region))
        if self._chapters_error:
            raise self._chapters_error
        return self._chapters


def _product(asin=ASIN, **over):
    return {**AUDIBLE_PRODUCT, "asin": asin, **over}


# ============================================================
# THE FROZEN LIST
# ============================================================

def test_a_pair_outside_the_list_is_refused():
    listed = _list((ASIN, "us"))

    assert listed.require(ASIN, "us") == 0
    with pytest.raises(ValueError, match="not in the frozen list"):
        listed.require(ASIN, "de")
    with pytest.raises(ValueError, match="not in the frozen list"):
        listed.require("9876543210", "us")


def test_a_list_holding_a_non_isbn10_asin_is_refused():
    with pytest.raises(ValueError, match="ISBN-10"):
        _list(("B0ABCDEFGH", "us"))


def test_a_duplicate_or_unknown_region_is_refused():
    with pytest.raises(ValueError, match="twice"):
        _list((ASIN, "us"), (ASIN, "us"))
    with pytest.raises(ValueError, match="region"):
        _list((ASIN, "xx"))


def test_a_list_round_trips_and_an_edited_one_is_refused(tmp_path):
    path = tmp_path / "list.json"
    _list((ASIN, "us"), ("012345678X", "de"), series_authors=[("B0S", "de", 4)]).dump(path)

    loaded = RepairList.load(path)
    assert loaded.pairs == [(ASIN, "us"), ("012345678X", "de")]
    assert loaded.series_authors == [("B0S", "de", 4)]

    document = json.loads(path.read_text())
    document["pairs"].append(["1111111111", "us"])
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="checksum"):
        RepairList.load(path)


def test_a_list_is_never_overwritten(tmp_path):
    path = tmp_path / "list.json"
    _list((ASIN, "us")).dump(path)

    with pytest.raises(FileExistsError):
        _list((ASIN, "de")).dump(path)


@pytest.mark.asyncio
async def test_run_refuses_a_pair_that_is_not_in_the_list(tmp_path):
    path = tmp_path / "list.json"
    _list((ASIN, "us")).dump(path)
    args = argparse.Namespace(
        list=path, backup=tmp_path / "b.jsonl", dry_run=False, limit=None,
        resume_from=None, pair=(ASIN, "de"),
    )

    with pytest.raises(ValueError, match="not in the frozen list"):
        await repair._run(args)
    assert not (tmp_path / "b.jsonl").exists()


# ============================================================
# WHAT A DRY RUN AND A NON-ANSWER MAY NOT DO
# ============================================================

@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing():
    listed = _list((ASIN, "us"))
    run = _Run()
    audible = FakeAudible(chapters=None, chapters_error=NotFoundException("none"))

    with patch.object(repair, "_replace_in_transaction", new=AsyncMock()) as replace, \
         patch.object(repair, "_snapshot", new=AsyncMock(return_value=_snapshot_of())), \
         patch.object(repair, "_invalidate", new=AsyncMock()) as invalidate:
        outcome = await repair._repair_one(
            _factory(), None, listed, audible, run, 0, {ASIN: _product()}, set(), True
        )

    assert outcome == "dry_run"
    replace.assert_not_called()
    invalidate.assert_not_called()
    assert run.repaired == 0 and run.dry_run_books == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kept,placeholders,reason", [
    ({}, set(), "no_product"),
    ({}, {ASIN}, "placeholder"),
])
async def test_a_non_product_answer_deletes_nothing(kept, placeholders, reason):
    listed = _list((ASIN, "us"))
    run = _Run()

    with patch.object(repair, "_replace_in_transaction", new=AsyncMock()) as replace:
        outcome = await repair._repair_one(
            _factory(), AsyncMock(), listed, FakeAudible(), run, 0, kept, placeholders, False
        )

    assert outcome == reason
    replace.assert_not_called()
    assert run.unrepaired == {reason: 1}


@pytest.mark.asyncio
async def test_an_answer_for_a_different_asin_deletes_nothing():
    listed = _list((ASIN, "us"))
    run = _Run()
    wrong = _product(asin="0000000000")

    with patch.object(repair, "_replace_in_transaction", new=AsyncMock()) as replace:
        outcome = await repair._repair_one(
            _factory(), AsyncMock(), listed, FakeAudible(), run, 0, {ASIN: wrong}, set(), False
        )

    assert outcome == "no_product"
    replace.assert_not_called()


@pytest.mark.asyncio
async def test_hollow_stubs_and_placeholders_are_told_apart_in_the_batch():
    stub = {"asin": "1111111111"}
    placeholder = _product(asin="2222222222", publication_datetime="2200-01-01T00:00:00Z")
    real = _product(asin=ASIN)
    audible = FakeAudible(products=[stub, placeholder, real])

    kept, placeholders = await _fetch_batch(audible, [ASIN, "1111111111", "2222222222"], "us")

    assert set(kept) == {ASIN}
    assert placeholders == {"2222222222"}


@pytest.mark.asyncio
async def test_a_404_on_the_product_is_an_empty_answer_and_an_outage_is_not():
    kept, placeholders = await _fetch_batch(
        FakeAudible(product_error=NotFoundException("x")), [ASIN], "us"
    )
    assert kept == {} and placeholders == set()

    with pytest.raises(repair._RunFailure):
        await _fetch_batch(FakeAudible(product_error=AudibleAPIException("down")), [ASIN], "us")


@pytest.mark.asyncio
async def test_a_chapters_404_or_empty_listing_is_none_and_an_outage_is_not():
    assert await _fetch_track(FakeAudible(chapters_error=NotFoundException("x")), ASIN, "us") is None
    assert await _fetch_track(FakeAudible(chapters={"content_metadata": {}}), ASIN, "us") is None

    with pytest.raises(repair._RunFailure):
        await _fetch_track(FakeAudible(chapters_error=AudibleAPIException("down")), ASIN, "us")


@pytest.mark.asyncio
async def test_an_outage_ends_the_run_with_the_cursor_held_and_nothing_replaced():
    listed = _list((ASIN, "us"), ("0123456780", "us"))
    run = _Run()

    with patch.object(repair, "_replace_in_transaction", new=AsyncMock()) as replace:
        await process(
            _factory(), AsyncMock(), listed,
            FakeAudible(product_error=AudibleAPIException("down")), run, _ThrottleSentinel(),
            start=0, end=2, dry_run=False,
        )

    assert run.abort_reason
    assert run.cursor == 0
    replace.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_replacement_holds_the_cursor_and_aborts():
    listed = _list((ASIN, "us"), ("0123456780", "us"))
    run = _Run()
    audible = FakeAudible(
        products=[_product()], chapters_error=NotFoundException("none")
    )

    with patch.object(repair, "_replace_in_transaction", new=AsyncMock(side_effect=RuntimeError("db"))):
        await process(
            _factory(), AsyncMock(), listed, audible, run, _ThrottleSentinel(),
            start=0, end=2, dry_run=False,
        )

    assert run.abort_reason and run.cursor == 0


# ============================================================
# THE BACKUP LINE
# ============================================================

def test_the_backup_line_carries_the_whole_prior_state():
    snapshot = _snapshot_of()

    record = _backup_record("sha", 7, ASIN, "us", snapshot)
    again = json.loads(json.dumps(record))

    assert again["type"] == "book" and again["list_sha"] == "sha" and again["index"] == 7
    assert (again["asin"], again["region"]) == (ASIN, "us")
    assert again["book"] == snapshot["book"]
    assert set(again["links"]) == {"author_book", "book_narrator", "book_series", "book_genre"}
    assert len(again["links"]["author_book"]) == 2
    assert again["links"]["book_series"][0]["position"] == "2"
    assert again["track"]["chapters"] == {"chapters": [{"title": "One"}]}
    assert again["foreign"] == {"author_ids": [2], "series": [["B0S", "de"]]}


def test_a_book_row_encodes_and_decodes_to_the_same_values():
    table = Book.__table__
    row = {
        "asin": ASIN, "region": "us", "title": "T", "explicit": False,
        "plans": None, "audible_extras": {"k": [1, 2]},
        "created_at": datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc),
        "rating": 4.25,
    }

    decoded = _decode_row(table, json.loads(json.dumps(_encode_row(row))))

    assert decoded["created_at"] == row["created_at"]
    assert decoded["audible_extras"] == {"k": [1, 2]}
    assert decoded["rating"] == 4.25
    # SQL NULL must come back as SQL NULL, not as the JSON value null.
    assert "plans" not in decoded


def test_the_backup_is_appended_flushed_and_exclusive(tmp_path):
    path = tmp_path / "b.jsonl"
    first = BackupFile(path)
    first.append({"format": 1, "type": "book", "list_sha": "s", "index": 0})

    # On disk before append returned, and a second run cannot share the file.
    assert len(read_backup(path)) == 1
    with pytest.raises(ValueError, match="locked"):
        BackupFile(path)
    first.close()


def test_a_torn_final_line_is_dropped_and_the_next_record_starts_clean(tmp_path):
    path = tmp_path / "b.jsonl"
    good = json.dumps({"format": 1, "type": "book", "list_sha": "s", "index": 0})
    path.write_text(good + '\n{"format": 1, "ty')

    assert len(read_backup(path)) == 1

    backup = BackupFile(path)
    backup.append({"format": 1, "type": "book", "list_sha": "s", "index": 1})
    backup.close()
    assert [r["index"] for r in read_backup(path)] == [0, 1]


def test_a_corrupt_line_that_is_not_the_last_is_an_error(tmp_path):
    path = tmp_path / "b.jsonl"
    good = json.dumps({"format": 1, "type": "book", "list_sha": "s", "index": 0})
    path.write_text("not json\n" + good + "\n")

    with pytest.raises(ValueError, match="line 1"):
        read_backup(path)


# ============================================================
# THE CURSOR
# ============================================================

def _records(*indexes, sha="s"):
    return [{"format": 1, "type": "book", "list_sha": sha, "index": i} for i in indexes]


def test_a_start_that_would_repeat_repaired_books_is_refused_not_run_from_zero():
    with pytest.raises(ValueError, match="--resume-from 3"):
        check_resume(_records(0, 1, 2), "s", 0)
    with pytest.raises(ValueError, match="--resume-from 3"):
        check_resume(_records(0, 1, 2), "s", 2)

    check_resume(_records(0, 1, 2), "s", 3)
    check_resume([], "s", 0)


def test_a_backup_from_another_list_is_refused():
    with pytest.raises(ValueError, match="different list"):
        check_resume(_records(0, sha="other"), "s", 5)
    with pytest.raises(ValueError, match="different list"):
        check_pair_unrepaired(_records(0, sha="other"), "s", 5)


def test_a_single_pair_already_repaired_is_refused():
    with pytest.raises(ValueError, match="already repaired"):
        check_pair_unrepaired(_records(4), "s", 4)
    check_pair_unrepaired(_records(4), "s", 5)


@pytest.mark.asyncio
async def test_the_run_resumes_at_the_cursor_and_never_before_it():
    pairs = [(f"012345678{i}", "us") for i in range(5)]
    listed = _list(*pairs)
    run = _Run()
    seen = []

    async def fake_repair(factory, backup, list_, audible, run_, index, *rest):
        seen.append(index)
        return "replaced"

    audible = FakeAudible(products=[_product(asin=a) for a, _ in pairs])
    with patch.object(repair, "_repair_one", new=fake_repair):
        await process(
            _factory(), AsyncMock(), listed, audible, run, _ThrottleSentinel(),
            start=2, end=5, dry_run=False,
        )

    assert seen == [2, 3, 4]
    assert run.cursor == 5
    assert audible.requests[0][1] == tuple(a for a, _ in pairs[2:])


def test_a_batch_never_mixes_regions_or_passes_fifty():
    pairs = [(f"{i:010d}", "us") for i in range(60)] + [(f"{i:010d}", "de") for i in range(3)]

    assert _next_batch(pairs, 0, 63) == 50
    assert _next_batch(pairs, 50, 63) == 60
    assert _next_batch(pairs, 60, 63) == 63
    assert _next_batch(pairs, 0, 10) == 10


# ============================================================
# IS_PRIMARY AND THE OTHER VALUES PUT BACK
# ============================================================

@pytest.mark.parametrize("primary", [True, False])
def test_the_rewritten_row_gets_its_original_is_primary_and_created_at(primary):
    stamp = datetime(2026, 10, 3, tzinfo=timezone.utc)

    values = _identity_values(_snapshot_of(primary=primary), stamp)

    assert values["is_primary"] is primary
    assert values["created_at"] == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
    assert values["chapters_checked_at"] == stamp


# ============================================================
# A STOP
# ============================================================

@pytest.mark.asyncio
async def test_a_stop_finishes_the_book_in_flight_and_holds_the_cursor_after_it():
    pairs = [(f"012345678{i}", "us") for i in range(4)]
    listed = _list(*pairs)
    run = _Run()
    seen = []

    async def fake_repair(factory, backup, list_, audible, run_, index, *rest):
        seen.append(index)
        if index == 1:
            run.request_stop()
        return "replaced"

    audible = FakeAudible(products=[_product(asin=a) for a, _ in pairs])
    with patch.object(repair, "_repair_one", new=fake_repair):
        await process(
            _factory(), AsyncMock(), listed, audible, run, _ThrottleSentinel(),
            start=0, end=4, dry_run=False,
        )

    assert seen == [0, 1]
    assert run.cursor == 2
    assert run.stopping and run.abort_reason is None


@pytest.mark.asyncio
async def test_a_stop_during_the_pause_cancels_the_request_unsent():
    run = _Run()
    audible = _Audible(run, get=AsyncMock())

    with patch.object(repair, "DELAY_MIN", 30.0), patch.object(repair, "DELAY_MAX", 30.0):
        task = asyncio.create_task(audible.products([ASIN], "us"))
        await asyncio.sleep(0.01)
        run.request_stop()
        with pytest.raises(repair._StopRequested):
            await asyncio.wait_for(task, timeout=2)

    audible._get.assert_not_called()


# ============================================================
# PACING AND THE EXIT
# ============================================================

def _fresh_module(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(
        "repair_region_pollution_probe",
        Path(repair.__file__),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_environment_can_slow_the_pacing_but_never_speed_it_up(monkeypatch):
    faster = _fresh_module(monkeypatch, REPAIR_DELAY_MIN="0.01", REPAIR_DELAY_MAX="0.02")
    assert (faster.DELAY_MIN, faster.DELAY_MAX) == (0.7, 2.0)

    slower = _fresh_module(monkeypatch, REPAIR_DELAY_MIN="3", REPAIR_DELAY_MAX="5")
    assert (slower.DELAY_MIN, slower.DELAY_MAX) == (3.0, 5.0)


@pytest.mark.asyncio
async def test_every_audible_request_is_preceded_by_a_pause():
    run = _Run()
    get = AsyncMock(return_value={"products": []})
    audible = _Audible(run, get=get)
    pauses = []

    async def record(seconds):
        pauses.append(seconds)

    run.sleep = record
    await audible.products([ASIN, "0123456780"], "us")
    await audible.chapters(ASIN, "us")

    assert len(pauses) == 2
    assert all(repair.DELAY_MIN <= p <= repair.DELAY_MAX for p in pauses)


def test_a_429_ends_the_run():
    sentinel = _ThrottleSentinel()
    run = _Run()
    record = logging.LogRecord("libex", logging.WARNING, __file__, 1, "x", None, None)
    record.status_code = 429
    record.attempts_left = 1

    sentinel.emit(record)
    _check_abort(run, sentinel)

    assert run.abort_reason and run.stopping


def test_sustained_5xx_ends_the_run_and_a_few_do_not():
    sentinel = _ThrottleSentinel()
    run = _Run()
    for _ in range(repair.ABORT_5XX_WITHIN - 1):
        record = logging.LogRecord("libex", logging.WARNING, __file__, 1, "x", None, None)
        record.status_code = 503
        record.attempts_left = 1
        sentinel.emit(record)
    _check_abort(run, sentinel)
    assert run.abort_reason is None

    record = logging.LogRecord("libex", logging.WARNING, __file__, 1, "x", None, None)
    record.status_code = 503
    record.attempts_left = 1
    sentinel.emit(record)
    _check_abort(run, sentinel)
    assert run.abort_reason


@pytest.mark.usefixtures("restore_audible_transport")
class TestDedicatedExitGuard:
    def test_refuses_the_shared_exit_and_logs_why(self, caplog):
        set_hosted_transport("socks5://user:secret@libex-vpn:1080")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as stop:
            _verify_dedicated_proxy()

        assert "libex-vpn" in str(stop.value)
        assert "secret" not in str(stop.value)
        assert "secret" not in caplog.text
        assert "refusing to start" in caplog.text

    def test_refuses_another_jobs_exit(self):
        set_hosted_transport("socks5://libex-refresh-vpn:1080")
        with pytest.raises(SystemExit):
            _verify_dedicated_proxy()

    def test_refuses_direct_egress(self):
        set_hosted_transport(None, allow_direct_egress=True)
        with pytest.raises(SystemExit):
            _verify_dedicated_proxy()

    def test_accepts_an_exit_named_repair(self):
        set_hosted_transport("socks5://libex-repair-vpn:1080")
        _verify_dedicated_proxy()
