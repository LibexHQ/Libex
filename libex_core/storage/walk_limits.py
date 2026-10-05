"""
The fixed numbers and names behind the stored walk snapshots, kept apart from
SQLAlchemy so the lookup layer can import them without the storage libraries.
"""

# Which walk a snapshot records. These are the two values the table's CHECK
# constraint admits.
AUTHOR_BOOKS = "author_books"
SERIES_BOOKS = "series_books"
WALK_KINDS = (AUTHOR_BOOKS, SERIES_BOOKS)

# The most book ASINs one snapshot may hold, well above the largest real
# walk. The writer never stores more, and the reader treats a longer list as
# a miss rather than truncating it.
MAX_WALK_ASINS = 20_000

# How far a stored confirmed_at may sit ahead of the local clock, in seconds,
# before it is no longer taken as "now". Absorbs clock drift between a store
# shared by several processes; beyond it the row is treated as future-dated,
# which the writer overwrites and the reader never serves.
SKEW_SECONDS = 300

# The longest an ASIN is, as written; checked on the raw string before any
# case folding can lengthen it.
ASIN_LENGTH = 12
