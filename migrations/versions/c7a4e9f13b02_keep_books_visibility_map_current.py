"""keep the books and tracks visibility maps current so the stats indexes stay usable

Revision ID: c7a4e9f13b02
Revises: 1203df663bc3
Create Date: 2026-09-05

books_region_asin_index (1203df663bc3) only pays for itself when Postgres can
run it as an index-only scan, and that needs the visibility map to mark the
scanned pages all-visible. That migration said as much and accepted a Bitmap
Heap Scan as the pre-VACUUM fallback. What it could not know is that on this
table the fallback was the steady state rather than a cold start, because
nothing was keeping the visibility map current.

Only VACUUM maintains relallvisible. ANALYZE never touches it, at any
frequency. That is the whole reason the two vacuum scale factors are the
lever here and autovacuum_analyze_scale_factor is not -- no amount of
analysing sets a visibility-map bit. At the defaults both vacuum rules sit
far apart: 0.2 of the live rows is roughly 360k changes between passes at
this table's 1.8M rows, on the dead-tuple rule and the insert rule alike.

Both rules apply, and the dead-tuple one is arguably the load-bearing half.
books is not append-only. The book upsert in services/db/writer.py is
on_conflict_do_update, so every ASIN the seeder re-sees leaves a dead tuple,
and re-seen ASINs are the majority of writes once a catalog has been walked
-- the seeder re-walks on a 24h interval. The chapters backfill in
services/audible/books.py adds a per-book UPDATE books SET
chapters_checked_at with its own commit, one more dead tuple per book across
the life of the backfill. Live confirmation: 1,086 dead tuples on books and
1,470 on tracks, hours after a full manual vacuum. The update traffic is
also the more expensive half, because an UPDATE un-sets the visibility bit
on a page that already holds committed rows, where an insert lands on a
fresh page that is cheaper to re-mark. Do not drop
autovacuum_vacuum_scale_factor here on the theory that only inserts happen.

What is measured and what is inferred, kept apart deliberately. Measured:
the map was stale and the counts were slow -- the incident, and the bench
below. Ruled out: a held-back xmin horizon, which would have made this
change inert. A manual VACUUM (ANALYZE) reached 99.4-100% all-visible on
every table, which it could not have done against a pinned horizon; the
codebase already guards that hazard in db/session.py's 60s
idle_in_transaction_session_timeout and in the persist queue's attempt cap;
and autovacuum and track_counts are both on. Inferred, not measured: that
the default 0.2 scale factors are why the map went stale. It is consistent
and sound on first principles, but the statistics that would have evidenced
it were discarded when the postmaster restarted for the shm_size deploy and
are not recoverable. For the same reason, last_autovacuum reading NULL is
evidence of nothing -- it means only that no autovacuum has run since that
restart.

The bench: a local 1.8M-row rebuild of books with the same index and the
production 30s statement_timeout, page cache evicted before every run. Map
unset, one uncontended count(*) took 31.9s and was killed by the timeout,
and 37 concurrent counts took 27.3s. Same table and same queries after a
single VACUUM (ANALYZE): 0.8s and 2.0s. On a clean copy the region-scoped
count read 1,425 buffers index-only against 450,000 for the heap scan it had
been doing. That rebuild was 4.4GB; production books is 337,935 pages, about
2.6GB, and tracks is 215,169 pages, about 1.7GB. The bench stands as a
bench, but it is a rebuild of this table and not this table.

1203df663bc3 priced the fallback at "roughly 2.6x the buffers" and that did
not reproduce -- but no single number replaces it either. The same
comparison measured 316x on a clean table and 1.5x on a bloated one, each
one bench on one shape, because the ratio moves with how bloated the index
itself is. The durable guidance is to read the plan Postgres chooses rather
than any multiplier: an index-only scan it declines to use costs the whole
heap however the buffers divide.

0.02 rather than the default 0.2 gives a pass every ~36k changes instead of
~360k. That is ten times more often for much less than ten times the cost,
because a normal VACUUM skips heap pages the map already marks all-visible:
the expensive pass is the first one, and every pass after it revisits only
what has been written since. That self-limiting argument covers the heap and
stops there. Whenever a pass has dead tuples to remove it also scans every
index on the table in full, and that cost scales with index size rather than
with pages changed; PG14+'s bypass -- skip index vacuuming when dead items
sit on under 2% of pages -- will usually not apply at this write rate.
Budget each pass at roughly the full index set plus the heap pages actually
dirtied. At the configured pacing that is one pass per ~20-60 minutes at
~150MB each, an order of magnitude cheaper than the heap scan it prevents.

Not 0.01: the per-pass index scan is a fixed cost and starts to dominate.
Not a raised autovacuum_vacuum_cost_limit either -- PG16 defaults
autovacuum_vacuum_cost_delay to 2ms rather than the PG11-era 20ms, which
already allows roughly 40MB/s of dirtying, so raising the limit would only
remove the brake that keeps autovacuum from competing with the seeder on an
I/O-constrained host. If passes ever bite, the lever is cost_delay upward.

tracks gets the same pair. It is smaller than books -- Track.asin is the
primary key, one row per book that has chapters -- but it is the worse table
on the numbers: 99.4% all-visible against 100.0 for books, 1,470 dead
tuples, and it backs booksWithChapters, whose region-scoped form joins
tracks to books and was measured at 4.7s. authors is deliberately left
alone: 99.8% all-visible, 63 dead tuples and 16 inserts since the last
vacuum, and at 7,508 pages the worst case if its map does go stale is a
~59MB scan, which is nowhere near the statement timeout. series and
narrators are tiny and sit at 100.0%.

This migration does NOT vacuum. VACUUM cannot run inside a transaction block
and migrations/env.py wraps every migration in one. It changes the policy
that keeps the maps current from here on; each existing table still needs
one manual `VACUUM (ANALYZE) books;` and `VACUUM (ANALYZE) tracks;` to set
its map the first time. Plain VACUUM, never VACUUM FULL -- plain VACUUM
takes no lock that blocks reads or writes, where VACUUM FULL takes ACCESS
EXCLUSIVE and rewrites the table.

That manual pass is a standing rule for a class of event, not a one-off for
this incident. A pg_restore or any bulk load lands a table with a completely
unset visibility map and no vacuum until the thresholds accumulate -- on an
instance where the seeder is off that never arrives, and /db/stats sits at
the statement timeout indefinitely. Vacuum after every restore or bulk load.

ALTER TABLE ... SET (reloptions) takes SHARE UPDATE EXCLUSIVE, which
conflicts with an autovacuum already running on the same table, and env.py
wraps all pending migrations in a single transaction, so a wait here stalls
startup migration as a whole rather than just this step. In practice that is
seconds: Postgres cancels a non-anti-wraparound autovacuum that blocks a
lock request.

Storage parameters only: no column, no data, no ORM attribute to match. The
reasoning is duplicated onto Book.__table_args__ beside the index it exists
to keep usable, and pointed to from Track, because that is where a reader
asking why the index underperforms will actually look.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c7a4e9f13b02'
down_revision: Union[str, Sequence[str], None] = '1203df663bc3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE books SET ("
        "autovacuum_vacuum_scale_factor = 0.02, "
        "autovacuum_vacuum_insert_scale_factor = 0.02"
        ")"
    )
    op.execute(
        "ALTER TABLE tracks SET ("
        "autovacuum_vacuum_scale_factor = 0.02, "
        "autovacuum_vacuum_insert_scale_factor = 0.02"
        ")"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE tracks RESET ("
        "autovacuum_vacuum_scale_factor, "
        "autovacuum_vacuum_insert_scale_factor"
        ")"
    )
    op.execute(
        "ALTER TABLE books RESET ("
        "autovacuum_vacuum_scale_factor, "
        "autovacuum_vacuum_insert_scale_factor"
        ")"
    )
