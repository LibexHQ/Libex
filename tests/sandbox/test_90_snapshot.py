"""Snapshot mode only (the root of this folder's conftest drops it otherwise)."""


def test_manual_entry_index_is_valid_and_the_listing_uses_it(sbx):
    valid = sbx.psql("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                     "WHERE c.relname = 'ix_books_manual_entry'")
    assert valid == "t"
    sbx.psql("ANALYZE books")
    query = ("SELECT * FROM books WHERE manual_entry_at IS NOT NULL "
             "ORDER BY manual_entry_at DESC, asin, region LIMIT 20")
    assert sbx.psql("SELECT count(*) FROM books WHERE manual_entry_at IS NOT NULL") != "0"
    assert "ix_books_manual_entry" in sbx.psql("EXPLAIN " + query)
    # The check can fail: with index access off the plan does not name it.
    off = sbx.psql("SET enable_indexscan = off; SET enable_bitmapscan = off; EXPLAIN " + query)
    assert "ix_books_manual_entry" not in off
