"""
Readers over the stored catalog, written against a SQLAlchemy session.

Every function takes the session first, reads no configuration and raises on
failure. A caller that wants a missing book and a broken database to look the
same must say so itself; nothing here turns an error into an empty answer.

The statements run unchanged on Postgres. On SQLite the few constructs with no
direct spelling there are swapped at compile time (see `_compat`), so the SQL
Postgres receives is not altered by SQLite being supported.
"""
