"""widen books with the remaining Audible product fields, and keep the new toast table vacuumed

Revision ID: a3f1c0d84b27
Revises: c7a4e9f13b02
Create Date: 2026-09-22

Eight nullable columns, no data touched. Measured on a 1.8M-row copy of
books: 2.57ms for the DDL and 4.22ms for the commit, with relfilenode and
relation size both unchanged afterwards. A nullable ADD COLUMN with no
default is a catalog write and nothing else, so this migration does not take
the service down however long the table has grown.

That property is fragile in exactly one direction, and it is worth naming
because the fix for a NULL column looks so harmless. A DEFAULT that is not
volatile is still rewrite-free -- DEFAULT '{}'::jsonb on this same table
measured 1.96ms -- but a volatile one is not: ADD COLUMN ... DEFAULT
gen_random_uuid() on the same 1.8M rows took 12,615ms and rewrote the whole
table. That 12,615ms is the figure to carry, not a multiple of it. Production
books is not larger than the copy it was measured on, on either axis that
matters: c7a4e9f13b02 measured production at 337,935 pages, about 2.6GB,
against 4.4GB for a 1.8M-row rebuild of this same table, and the corpus it
holds is about 1.8M books, the same order as the copy. If anything the
rewrite runs faster there. Duration was never the hazard -- it is a full
table rewrite holding ACCESS EXCLUSIVE, so every reader queues behind it
however long it takes.

No default of any kind is used here regardless, and that is a data decision
rather than a performance one. audible_extras carries three states and all
three mean something: NULL is "no response has written this row since the
column existed", '{}' is "Audible was asked and sent nothing beyond the
fields books already reproduces", and a populated object is the remainder of
what Audible sent. An existing row acquires the blob only when some later
response rewrites that row, so NULL here is not a transient state that clears
itself on its own -- it stands until the row is next written, which is what
makes it worth keeping. DEFAULT '{}'::jsonb would cost nothing to apply and
would still be wrong: it stamps every existing row as answered-and-empty
before anything has asked, collapsing the first state into the second
permanently and leaving no way to tell afterwards which rows had ever really
been asked. That loss falls on anyone reading the data rather than on any one
process. With no default, `audible_extras IS NULL` answers how much of the
corpus has been rewritten since this landed; with the default applied, the
question cannot be asked at all.

No backfill UPDATE in here either -- it would spend that same distinction on
the way in, and migrations/env.py wraps the entire upgrade in one
engine.begin(), so an UPDATE over 1.8M rows would run inside the transaction
that gates application startup.

lock_timeout is set because this connection has none of the safeguards a
request connection has. env.py builds its own engine from the database URL
with no connect_args at all, so neither the 30s statement_timeout nor the 60s
idle_in_transaction_session_timeout configured in app/db/session.py applies to
a migration. The DDL itself is microseconds; what needs a bound is acquiring
the lock. ADD COLUMN takes ACCESS EXCLUSIVE, which conflicts with an ordinary
SELECT, so during a rolling deploy this can queue behind a long read and then
hold every subsequent read behind itself. c7a4e9f13b02 reasoned that a wait
for its own lock would be "in practice seconds" because Postgres cancels a
blocking autovacuum -- that argument is about SHARE UPDATE EXCLUSIVE against
autovacuum and does not carry here. Nothing cancels a live query. Failing
after three seconds and being re-run is the better outcome than succeeding
after ninety with the service stopped behind it. The setting is reset at the
end so it does not silently shorten the patience of whatever migration runs
next in the same transaction.

The toast reloptions are the half of this migration that is not about the
columns, and the reasoning is c7a4e9f13b02's own incident rather than a
restatement of its conclusion. That migration lowered autovacuum's two vacuum
scale factors on books from the default 0.2 to 0.02 because only VACUUM
maintains the visibility map -- ANALYZE never sets a bit, at any frequency --
and books takes update traffic as well as inserts: the book upsert is
on_conflict_do_update, so every re-seen ASIN leaves a dead tuple, and the
chapters backfill stamps chapters_checked_at one book at a time. At 0.2 that
is roughly 360k changes between passes on a 1.8M-row table, the map went
stale, and Postgres stopped choosing the index-only scan that
books_region_asin_index exists for. Measured on a rebuild of this table with
the production 30s statement_timeout and the page cache evicted: one
uncontended region-scoped count(*) took 31.9s and was killed by the timeout,
and 37 concurrent counts took 27.3s. After a single VACUUM (ANALYZE), 0.8s
and 2.0s.

TOAST does not inherit any of that. A heap-level ALTER TABLE books SET
(autovacuum_vacuum_scale_factor = 0.02) leaves the toast table's reloptions
NULL, and the toast table keeps running at the global 0.2. Until now that was
academic, because books had little worth toasting. audible_extras changes it:
the blob is the remainder of an entire Audible product, so the moment this
migration lands books acquires a multi-gigabyte toast table sitting at exactly
the setting that produced the incident above, on the one table explicitly
hardened against it. The toast-level pair below closes that, and the
downgrade resets it in step with dropping the column that made it necessary.

No index on any of the eight. They are projected rather than searched: a book
row carries them out to the caller, but none of them appears in a WHERE, an
ORDER BY or a join anywhere in the application, and the row they belong to is
already being found by primary key. There is no lookup here for an index to
serve. Against that, a GIN index on audible_extras would put its pending-list
flushes directly on the seeder's hottest write path -- the full cost of the
index with none of its benefit. A partial index on `WHERE audible_extras IS
NULL` was considered and left out on the same ground: the progress query above
is an operator asking a question occasionally, and an index to serve it would
be maintained on every write to the table and read from almost never.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a3f1c0d84b27'
down_revision: Union[str, Sequence[str], None] = 'c7a4e9f13b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column('books', sa.Column('num_ratings', sa.Integer(), nullable=True))
    op.add_column('books', sa.Column('num_reviews', sa.Integer(), nullable=True))
    op.add_column('books', sa.Column('publication_name', sa.Text(), nullable=True))
    op.add_column(
        'books',
        sa.Column('publication_datetime', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('books', sa.Column('extended_product_description', sa.Text(), nullable=True))
    op.add_column('books', sa.Column('product_state', sa.Text(), nullable=True))
    op.add_column('books', sa.Column('audible_extras', postgresql.JSONB(), nullable=True))
    op.add_column('books', sa.Column('extras_withheld', postgresql.JSONB(), nullable=True))
    op.execute(
        "ALTER TABLE books SET ("
        "toast.autovacuum_vacuum_scale_factor = 0.02, "
        "toast.autovacuum_vacuum_insert_scale_factor = 0.02"
        ")"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "ALTER TABLE books RESET ("
        "toast.autovacuum_vacuum_scale_factor, "
        "toast.autovacuum_vacuum_insert_scale_factor"
        ")"
    )
    op.drop_column('books', 'extras_withheld')
    op.drop_column('books', 'audible_extras')
    op.drop_column('books', 'product_state')
    op.drop_column('books', 'extended_product_description')
    op.drop_column('books', 'publication_datetime')
    op.drop_column('books', 'publication_name')
    op.drop_column('books', 'num_reviews')
    op.drop_column('books', 'num_ratings')
    op.execute("RESET lock_timeout")
