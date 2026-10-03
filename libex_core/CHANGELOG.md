# Changelog

All notable changes to `libex_core` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`libex_core` is below 1.0, so this project borrows post-1.0 MAJOR
discipline into the MINOR slot rather than relying on SemVer's 0.x carve-out:
while pre-1.0, MINOR carries any breaking change, and PATCH is reserved for
fixes that have no effect on the package's public surface.

`libex_core` is published on PyPI as the `libex-core` distribution. 0.20.0 is
the first published version. Entries for earlier versions record changes made
before publication: they are historical record for whoever embeds this package,
not evidence that anyone consumed a given version at the time it was cut.

## [0.21.0]

### Added
- **`LibexClient` can go through a SOCKS5 proxy.** A `socks5://` or `socks5h://` proxy URL is now accepted, and the two behave the same: the proxy is always handed Audible's hostname and resolves it, and the name is never resolved on this machine. TLS to Audible stays end to end through the tunnel. A SOCKS5 URL must name its port, because SOCKS has no default one and none is guessed. `socks4://`, `socks4a://` and any other scheme are still refused. A username and password in a SOCKS5 URL are sent to the proxy as RFC 1929 username/password authentication, which is cleartext between this machine and the proxy, so use them only on a network you trust.
- **A `socks` extra installs what SOCKS needs.** `pip install "libex-core[socks]"` adds the SOCKS support for the HTTP client. Without it, building a client with a SOCKS5 URL raises `ValueError` at construction, naming the extra, rather than failing on the first request.
- **`TransportSummary` has a `scheme` field.** It is the proxy's scheme (`http`, `https`, `socks5` or `socks5h`) when a proxy is configured and `None` for direct egress. It never carries the URL or credentials. Existing fields are unchanged.
- **`LocalStore(url, *, connect=None)` takes a hook that makes the database connection for you.** For a connection libex-core cannot make itself, such as a tunnel, a short-lived token or an encrypted SQLite build. On PostgreSQL the hook takes no argument and returns an awaited `asyncpg.Connection`, and the URL must then be the bare `postgresql+asyncpg://`; a host, user, password, database, port or option in it raises `StoreConfigError`. On SQLite the hook is an ordinary function, `(path) -> sqlite3.Connection`, called with the path libex-core has already checked (a symbolic link is refused and on Linux and macOS a new file is created with mode 0600); it may return a build with the same interface, such as SQLCipher. libex-core wraps the connection in the async one itself, so its worker thread is a daemon and a store that is never closed does not keep the interpreter from exiting. The connection is read once before use, so a wrong encryption key is a `StoreConnectionError`. Write-ahead logging is switched on afterwards, over the connection the hook returns. The schema checks, the refusal of a database this package did not create, upgrades, write locking and the SQLite foreign-key handling all apply to the connections the hook returns. Without `connect` nothing changes.
- **With a hook, TLS and credentials are yours.** libex-core cannot manage or check the PostgreSQL TLS mode and certificate verification, whether `PG*` variables and `~/.pgpass` are read, a password being resent in plain text after a failed encrypted attempt, `gsslib`, `krbsrvname` or `server_settings`. `PYPI.md` lists them.
- **A connection that fails its setup is closed, for every store.** When the database accepts a connection but SQLAlchemy's setup of it then fails, the connection used to be left open until it was garbage collected. It is now closed straight away, on SQLite and PostgreSQL, with or without a hook; a failure on a connection opened inside `store.session()` or `store.write()` closes it as that block ends, and anything still unconfirmed is closed by `store.close()`.
- **`LocalStore.connection_mode` reads `"managed"` or `"caller"`.** It is read-only, appears in `repr` beside the backend, and a store with a hook logs one INFO line saying connections are supplied by the caller and TLS and credentials are not managed by libex-core. `repr` still never shows the URL.
- **A hook that raises, or returns the wrong type, is a `StoreConnectionError`.** The message names only the exception class, never what the hook or the driver said, which may hold a password. A `connect` that is not callable is `StoreConfigError`.
- **The store keys books, series and chapters by ASIN and region, and needs a migration: run `libex-core db upgrade`.** The same ASIN returned by two marketplaces is now two rows, each with its own authors, narrators, genres, series links and chapters, and the link tables carry the region of the book or series they point at. Until the store is upgraded, `open()` refuses it as `StoreOutdated`, as for any older schema. The upgrade fills the new region columns from the region the row it points at already has, which is never ambiguous in a store that was keyed by ASIN alone. It does not repair: a series with no region, or a link row whose book or series is gone, stops it with a count, and nothing is changed. On SQLite the tables that change keys are rebuilt. Downgrading refuses, with a count, once any ASIN is stored under more than one region, and deletes nothing.
- **`libex-core db book`, `db chapters` and `db series` take `--region`.** With it, that marketplace's record, or exit 3 if it has none. Without it, the record stored first, which is the same one every time. `db stats` also prints `distinctBookAsins`, the number of ASINs among the stored books; `books` still counts stored records, so it is larger whenever a book is stored under more than one region.
- **`write_track` no longer raises when the book is not stored for the region.** A chapters write for a book the store does not hold under that region is skipped and `write_track` returns `None`, where it would have failed on the foreign key to books. `lookup` with a `store` logs the skip at info level and the chapters are returned to the caller unstored, as before.
- **The store has new nullable columns and a new table that nothing uses yet.** `confirmed_at` on books, series and authors, `chapters_confirmed_at` on books, and a `walk_results` table. The upgrade creates them empty; nothing reads or writes them and no output carries them.

### Changed
- **The proxy refusals now name the SOCKS5 schemes.** A proxy URL with an unsupported scheme raises `ValueError` saying it must use the http, https, socks5 or socks5h scheme, where it used to say only http or https. A `ValueError` still never contains any part of the URL. The `LIBEX_CORE_PROXY_URL` help, the command line's configuration error and the man page list the four schemes, and the error notes that a SOCKS5 URL needs its port and the `socks` extra.
- **A second marketplace's record is now stored, and the cross-region skip is gone.** With a `store`, a lookup used to leave a book, a series or its chapters unwritten when the store already held that ASIN for another region, and logged a warning. It now writes the asked region's own record beside the other, and the warning no longer exists. A series link is no longer left out of a write for that reason either.
- **Stored reads and outage answers are scoped to the region asked.** A lookup that answers from the store when Audible cannot be reached, and `get_series`, `search_series`, `get_chapters` and the book lookups behind it, read only the asked region's record. Called without a region, the storage readers return the record stored first for each ASIN, so a list holds one row per ASIN. `get_author_books` is unchanged in what it returns: with no `book_region` every stored book linked to the author is returned, whatever its region, and `book_region` narrows the list to one marketplace's records. Books of a SKU group are returned for every stored region, ordered by region and then ASIN. In the region-scoped `count_stored`, `seriesRegionUnknown` is kept for compatibility and is always 0, since a series always has a region now.
- **Breaking for direct users of the storage writers: `write_track` now requires `region=`, and a series needs a region to be written.** `write_track(session, asin, chapters, *, region, ...)` has no default, because a listing filed under the wrong marketplace is a silent error; a call without it raises `TypeError`. `write_series_profile` returns `None`, and `upsert_series` writes nothing, for a series that names no region, and a series that arrives through a book's relationships takes the book's region. `write_books` needs each book to carry its `region`, and `resolve_author_ids` is keyed by `(asin, region)` rather than by ASIN. Code that only uses `lookup` with a `store`, or the command line, is not affected.

### Fixed
- **A book's link to a series is kept when the series entry has no name or title.** An entry with an ASIN and a position but no name or title used to drop its link to the book, and the position, even when that series was stored. The link is now written whenever that series is stored for the region; one naming a series not stored for the region makes no link and loses nothing else.

## [0.20.0]

### Added
- **Every `libex_core.lookup` function takes a keyword-only `store`, a `LocalStore`.** Without it nothing changes: nothing is stored and an outage is an outage. With it, what Audible answered is written to the store under the same merge rules the hosted service uses (a stored value is never replaced by less, and the links between records only grow), and the book, series or author returned is the row the store then holds, not the raw answer, with one addition: a book served from its stored row also carries Audible's series entry for any link left out of the write (see below), in Audible's order with the row's own entries after them. The store keys books and series by ASIN alone, so a row belongs to one region, and one stored for another region is never overwritten and never served for this one. What the store hands back after a write, and what it hands back when Audible cannot be reached, is only ever a row stored for the region asked; when a stored row cannot be served (it belongs to another region, the book was skipped, or the write failed) the caller gets Audible's live book. A write skips, per ASIN, a book whose stored row belongs to another region, together with its links and chapters, and a series profile is gated the same way. A series link from a book to a series stored for another region is left out of the write, so that series' title, description and update time are not touched through it and the link is not added. The book is still served with Audible's series entry for that link, so the caller does not lose it; only what is stored is limited. Each skip logs a warning, and the rest of a list is written as usual. The check runs inside the write, so a concurrent writer cannot slip past it. One window remains, on PostgreSQL: two transactions that first insert the same new ASIN for different regions at the same moment cannot see each other, and the row keeps the region of whichever commits first. A store that is closed or was never opened is refused with `StoreClosed` before anything is requested. The storage libraries are imported only when a store is passed, so `libex_core.lookup` still works without the `storage` extra.
- **With a store, a lookup answers from it when Audible cannot be reached.** `get_book`, `get_books`, `get_chapters`, `get_series`, `get_series_books` and `get_author` return the stored copy instead of raising, and `get_books` fills an ASIN whose request failed from the store, leaving in `notFetched` only what the store lacks too. Only records stored for the region asked are used: a book ASIN is region-specific, so a copy another marketplace stored is not offered in its place. When some books of a `get_books` or `get_series_books` list were answered from the store, the whole list is in the order requested (series order for a series) rather than Audible's order. When Audible confirms it has no record, that is still `NotFoundException` (or `notFound`, or `placeholderRecords`), never overruled by stored data, and a lookup that fails with nothing stored raises as before.
- **The stored copy answers in a few more places, and not in others.** `search_series` adds the stored series whose names match, after Audible's, up to ten. `get_author_books` by ASIN unions the ASINs of the author's stored books into discovery on every call, and takes the author's name from the stored profile when there is one, so what was found once is not lost to a thin walk; a stored read that fails counts as a failed discovery source, not as an author with no books. `quick_search` and the Audiobookshelf quick search try the stored books for an `Author - Title` query that the catalog could not answer. A catalog search, the catalog step of an author search by name, a release window and the author-books-by-name walk are not answered from the store when Audible cannot be reached: they raise as before. `new_releases`, `coming_soon` and the searches write the books they find. `categories` accepts `store` and does nothing with it: the taxonomy is never stored.
- **Chapters are stored only for a book already in the store.** A chapter listing hangs off its book's record, so `get_chapters` with a store keeps the richer of the stored and the offered listing when the book is held, and otherwise returns Audible's listing unstored. A book held for another region does not count as held: its chapters are not stored.
- **A failed write is reported, not raised.** The live answer is still returned and the failure is logged. `BookList` (the author-books and series-books results) and `Hydration` carry `store_write_failed`, True when a fetched book could not be written, and `from_store`, the ASINs of the books answered from the store because Audible could not answer for them. Every answer given from the store during an outage is logged as a warning.
- **`LIBEX_CORE_STORAGE` turns the local store on for the command line.** `off` or empty, the default, keeps nothing. `sqlite` uses `libex.db` in the platform's per-user data directory; an absolute path uses that SQLite file; a `sqlite:///` URL with an absolute path or a `postgresql://` URL uses that database. A relative path, a relative SQLite URL and an in-memory SQLite database are refused. The value is read from the environment only and never printed. SQLite needs the `storage` extra, and PostgreSQL the `postgres` extra.
- **New `libex-core db` commands read the store.** `db upgrade` creates or brings the schema up to date, and is the only command that changes its structure; `db status` prints whether the store is `ok`, `not-initialised`, `outdated`, `ahead` or `foreign`, with its revision, and exits 0 only for `ok`. `db book`, `books`, `chapters`, `sku`, `author`, `author-books`, `series`, `series-books`, `narrators`, `narrator-books`, `genres`, `plans`, `plan`, `vvab`, `new-releases`, `coming-soon` and `stats` each print JSON in the shape of the matching hosted stored-data route, make no request to Audible and need no proxy. `book sku` prints the stored books of a SKU group and is answered from the store alone. A command that finds nothing exits 3.
- **With `LIBEX_CORE_STORAGE` set, the lookup commands open the store first and store what they fetch.** A store that is not ready stops the command before any request is made. A lookup answered from the store because Audible was unreachable is printed as a success.
- **The package has a PyPI long description**, and the documented public modules (`libex_core.asin`, `libex_core.audible.client`, `libex_core.exceptions`, `libex_core.models`) declare `__all__`. Importing `libex_core` itself still exposes only `__version__`; use the module that holds the name. `libex_core.models` also gains `NarratorProfileResponse` and `AudioSampleResponse`, the narrator profile shape, which is what `db narrators` prints.

### Changed
- **`get_series_books` can report `discovery-incomplete`.** When Audible could not give the member list and stored members answer in its place, the list is returned with `complete` False and `discovery-incomplete`, because nothing confirms the store holds the whole series. Without a store a series still never reports it, and `hydration-deadline` never appears for a series.
- **The command-line exit status 5 now also covers storage.** It is returned when storage is off for a `db` command or `book sku`, when the `storage` or `postgres` extra is missing, when the store is unreachable or not ready, and when the store's file or directory cannot be read, created or written. That last message is fixed text and does not include the path.

## [0.19.0]

### Added
- **`libex_core.storage.LocalStore(url)` is a Libex database you own, on SQLite or Postgres.** It takes a `sqlite` or `sqlite+aiosqlite` URL, or a `postgresql` or `postgresql+asyncpg` URL, and nothing else. `session()` gives a read session, and `write()` gives a write session that is serialised against other writers, commits when the block ends and rolls back if it raises. The store is an async context manager; `close()` releases it and is safe to call twice. `LocalStore` and its errors (`StoreError`, `StoreConfigError`, `StoreConnectionError`, `StoreNotInitialised`, `StoreOutdated`, `ForeignDatabase`, `StoreClosed`, `StoreMigrationError`) are exported from `libex_core.storage`.
- **`upgrade()` is the only thing that creates tables.** It runs the package's own migration chain, which records its progress in its own version table, so it does not meet a hosted Libex's migrations. It runs in one transaction, so a failure leaves the database as it was, and returns the revision it reached, logging the outcome. SQLite migrations run with foreign keys off and are checked afterwards: if a migration would leave rows that break a foreign key it is rolled back and `upgrade()` raises `StoreMigrationError`, a `StoreError` that carries no row data. A new SQLite file is created with mode 0600, and its folder, if new, with mode 0700 (Linux and macOS); a file that already exists is left as it is, with a warning if other users can read it. SQLite is switched to write-ahead logging during the upgrade. On SQLite, the region and genre type columns carry a CHECK constraint, so a value outside the allowed set is refused as Postgres refuses it.
- **`open()` never upgrades, and refuses a database that is not ready.** An empty database raises `StoreNotInitialised`. A database that has tables but no record of this package's migrations, which includes a hosted Libex database, raises `ForeignDatabase` and is not touched. A database behind this version raises `StoreOutdated`, and so does one made by a newer `libex-core`. `upgrade()` refuses a foreign or newer database the same way. `status()` reports where a database stands without changing it, and does not create a missing SQLite file.
- **URLs are checked before anything connects.** A SQLite URL takes a file path and nothing else: no query string, host, credentials or `file:` URI, and a symbolic link or a non-file path is refused. A Postgres URL needs a host, a user and a database name, and the port defaults to 5432. Its only query keys are `ssl` (`disable`, `allow`, `prefer`, `require`, `verify-ca` or `verify-full`, default `prefer`) and `application_name`. Anything else raises `StoreConfigError`.
- **Errors never contain the URL or the password.** Messages say what is wrong without quoting the URL, and `repr` shows only the backend. `StoreConnectionError` carries the name of the driver's error class and nothing the driver said.
- **A Postgres connection takes its settings only from the URL.** The `PG*` environment variables and `~/.pgpass` are never read, and neither are a service file or `~/.postgresql`. A URL without a password connects without one. The certificate trust store used by `verify-ca` and `verify-full` is the system's, which `SSL_CERT_FILE` and `SSL_CERT_DIR` can replace, so those two variables still affect a connection.
- **The default `ssl=prefer` does not protect against an active attacker.** `prefer`, `allow` and `require` do not verify the server's certificate (`allow` also tries an unencrypted connection first), so someone who can intercept the connection can read or alter it. On a network you do not trust, use `verify-full`.
- **A failed login is not retried over the other transport.** Under `prefer` and `allow`, the plain (or encrypted) attempt happens only when the server refuses the first transport; a wrong password or other rejected login raises `StoreConnectionError` straight away.
- **Postgres needs the `postgres` extra.** If asyncpg is not installed, `LocalStore` raises `StorageUnavailable`, whose message names `libex-core[postgres]`.

No command in this release uses a store, and nothing opens one unless the caller does.

## [0.18.0]

### Added
- **`libex_core.lookup` now covers series search, authors, an author's books, new releases, coming soon and categories.** Eight more lookups join the ten from 0.14.0, in the same shape (the request callable first, `region` a keyword defaulting to `us`, the published model back). `search_series(get, name)` returns a list of `SeriesResponse`. `get_author(get, asin)` returns an `AuthorResponse` and `search_authors(get, name)` a list of them. `new_releases` and `coming_soon` take `days` and an optional `category` and return a list of `BookResponse`; `categories` returns the genre tree. As before they are the hosted live path without the cache or the database: Audible's answer is normalized and settled, and nothing is stored.
- **`get_author_books` and `get_author_books_by_name` return a `BookList`: `books`, `complete` and `incomplete_reasons`.** The hosted routes tell a caller whether a list is whole in the `X-Libex-Complete` response headers; a library caller has none, so the answer carries it. `complete` is False when discovery stopped before confirming it had every ASIN, or when fewer books came back than ASINs were found, and `incomplete_reasons` then names why, drawn from `INCOMPLETE_REASONS`: `discovery-incomplete`, `hydration-deadline`, `hydration-failed` and `hydration-not-found`. It is empty exactly when `complete` is True. A list that is not complete is still returned, since what was gathered is worth having; nothing here retries or finishes it afterwards. Those judgements are made before any filter, so filtering a list shorter does not make it incomplete. By ASIN, discovery and the hydration that follows share one 25 second budget, and a prolific author's lookup that reaches it comes back incomplete with `hydration-deadline` or `discovery-incomplete`. By name, the catalog is searched on the exact name, ignoring case, and a walk that stopped before a confirmed end returns what it gathered with `discovery-incomplete`. Without a store, the author's books are those Audible lists: unlike the hosted route, there are no stored books to union in.
- **`new_releases` and `coming_soon` take `days` of 30, 60, 90, 120, 240 or 365 (`RELEASE_WINDOWS`, default 30) and an optional numeric `category` id.** Without a category the result is a live sample of the window, not all of it, because Audible caps an uncategorized catalog query at a few hundred results. A category scopes the walk, and the ids come from `categories`. Nothing fans out across categories on its own; reaching a whole window means one call per category and merging the results. `new_releases` returns newest first and skips pre-orders; `coming_soon` returns soonest first and skips titles already out. A `days` outside the windows or a `category` that is not numeric is `ValueError`, before any request, and the message does not repeat the value.
- **`categories(get, *, region, flat, depth)` returns Audible's genre taxonomy** as a nested tree of `CategoryNode`, or with `flat=True` a flat list of `FlatCategoryNode` in which each node carries its ancestors root-first. `depth` limits the levels (1 is the top level only); below 1 is `ValueError`. It is fetched live on every call.
- **`get_books` and `get_series_books`, and the new author-books and release lookups, take `filters`, `sort` and `order`.** The filter names and sortable fields are those of `libex_core.shaping`. A filter or sort outside them, or an `order` other than `asc` or `desc`, is `ValueError` before any request is made. Shaping applies to the books only: in `get_books` it runs after `notFound`, `placeholderRecords` and `notFetched` are worked out, so a book that was found but filtered out is never reported missing. Defaults follow the hosted routes: `asc`, except `new_releases`, which is `desc`; with no `sort`, the books keep the order the lookup produced.
- **New `libex-core` commands: `series search`, `author get`, `author search`, `author books`, `author books-by-name`, `releases new`, `releases coming-soon` and `releases categories`.** Each prints JSON on standard output and takes `--region`. `releases new` and `releases coming-soon` take `--days` (the six windows, default 30) and `--category ID`; `releases categories` takes `--flat` and `--depth` (1 to 9). `book bulk`, `series books`, `author books`, `author books-by-name`, `releases new` and `releases coming-soon` take one flag per filter (for example `--language`, `--genre`, `--longer-than`), plus `--sort` and `--order`. `series books`, `author books` and `author books-by-name` print the list of books alone, as the hosted routes do; when the list may not be whole it is still printed, a one-line notice naming the reasons goes to standard error, and the status is 0. Exit statuses are as before: 2 for a rejected argument, 3 for nothing found, 4 for Audible unreachable.
- **The generated completions and man page cover the new commands, their flags and their values.**

### Changed
- **`get_series_books` returns a `BookList` instead of a list of `BookResponse`.** In 0.14.0 it returned the list itself; the books are now in `.books`, beside `complete` and `incomplete_reasons`, the same shape the author-books lookups return. Callers that iterated, indexed or took the length of the result must use `.books`. `complete` is False, with `hydration-failed` and/or `hydration-not-found` in `incomplete_reasons`, when a member's request failed or Audible has no record of it; what was gathered is still returned. The member list is one request, so `hydration-deadline` never appears for a series, and `discovery-incomplete` appears only when a store answers in place of a member list Audible could not give, as the entry for store-backed lookups describes. Completeness is judged before any filter. The `series books` command now prints the books alone and sends the one-line incompleteness notice to standard error, as the author commands do.
- **A `category` is checked more strictly.** A value with a trailing newline is no longer accepted as numeric; it is `ValueError`, as any other non-numeric value is.
- **Every lookup reports an outage with the same message, "Audible unavailable".** The author-books lookups previously said "Audible unavailable for author books". Branch on the exception type, not its text.
- **Two lookups differ from the hosted routes, on purpose.** A 404 from Audible while building new releases, coming soon or categories raises `NotFoundException`; the hosted routes answer 503 there. `search_series` and `search_authors` raise `AudibleAPIException` when candidates were found and every one of them failed to fetch, because an empty list would pass an outage off as an absence; the hosted routes answer 404 there. When nothing was found at all, both raise `NotFoundException`.
- **The command line no longer repeats what was typed when it refuses something.** A value that is not one of the allowed choices is refused with the list of choices and without the value, and unknown arguments are refused with a fixed message that does not list them. Previously both were quoted back.
- **Flag abbreviations are no longer accepted.** A flag must be given by its exact name: `--reg us` for `--region us`, which worked in 0.14.0, is now refused as an unrecognized argument.

## [0.17.0]

### Added
- **`libex_core.storage.read` reads the stored catalog.** It covers books (by ASIN, in bulk, by SKU group, search, plan, VVAB, new releases, coming soon, the distinct plans and genres, and tracks), authors and their books, narrators and their books, series and their books, and `count_stored` for per-table counts. Each function is async and takes a SQLAlchemy session as its first argument, and the results are the same dictionaries the hosted service serves from its stored-data routes. The building blocks that produce those dictionaries (book, narrator and series-position shaping) are in `libex_core.storage.read.shapes`.
- **The readers raise on failure.** A broken database raises; it is never turned into an empty result, so a caller can tell a missing book (`None` or an empty list) from a database that could not be read.
- **`libex_core.storage.filtering` and `libex_core.storage.sorting` build the filters and the sort for those reads.** `apply_book_filters`, `apply_narrator_filters`, `apply_genre_filter`, `apply_category_filter` and `apply_sort` take and return SQLAlchemy statements, and the sort allow-lists for books and narrators are published there.
- **The reads work on SQLite as well as Postgres.** Case-insensitive matching, JSON containment and key checks, series-position classification and putting missing values last each have a SQLite equivalent. The statements Postgres receives are unchanged.

### Changed
- **Known difference: text sort order is not the same on both backends.** Sorting by title, publisher, narrator name or a non-numeric series position follows the database's locale collation on Postgres and plain byte order on SQLite, so mixed-case or accented text can come back in a different order.
- **Known difference: a search pattern ending in a lone backslash.** On Postgres it is an error; on SQLite it is accepted and matches nothing.
- **Known difference: series-position ordering and non-ASCII digits.** SQLite treats only ASCII digits (0-9) as digits when deciding whether a series position is numeric. Postgres's `\d` may also accept other Unicode digits, depending on locale. This affects series-position ordering only.
- **Known difference: distinct plan names that are not strings.** Plan names are strings by contract. If a plan list holds a non-string JSON element, SQLite renders it with its own spacing, which can differ from Postgres's.
- **SQLite version requirements.** The reads need SQLite 3.30 or later for `NULLS LAST`, and the JSON1 functions, which are built in from 3.38.

Still groundwork: nothing opens a database or creates tables yet, and no command uses these readers. They need a session you have built yourself over tables you have created.

## [0.16.0]

### Added
- **`libex_core.storage.dialect` prepares a SQLite engine to run the same SQL the hosted service runs on Postgres.** `configure_sqlite(engine)` turns on foreign key enforcement, which SQLite leaves off, and a busy timeout so two processes sharing one file queue rather than fail on the first overlap. File databases are put in write-ahead logging mode, and writes open with `BEGIN IMMEDIATE` so a transaction that reads and then writes cannot find the write lock taken from under it. It also registers a `lower()` that matches Postgres and the JSON containment and merge functions the merge rules need. The JSON functions refuse a stored document nested more than 200 levels deep with a `ValueError`. It accepts a sync or an async engine, is safe to call twice on the same one, and raises `ValueError` for an engine that is not SQLite. SQLite 3.35 or newer is required: below that, `configure_sqlite` and `require_sqlite_version()` raise `SQLiteTooOld`, naming both versions.
- **`libex_core.storage.merge` holds the keep-the-richer-data merge rules, and they give identical results on SQLite and Postgres.** Stored data is never replaced by less. A blank or missing incoming value keeps what is stored, and a longer description wins over a shorter one. Extras only grow: a thinner response adds keys to a richer stored set and never replaces it. Links between books and authors, narrators, genres and series are only added, never removed. Chapters keep the richer list: a response with no chapters cannot erase a stored list, and a later response that has chapters replaces it whole.
- **`libex_core.storage.write` writes normalized Audible responses into the stored schema.** It provides `write_books`, `upsert_author`, `upsert_genre`, `upsert_narrator`, `upsert_series`, `write_author_profile`, `write_series_profile` and `write_track`. Each takes the session first, reads no settings or environment, never commits, and raises on failure; the caller owns the transaction. `exclusive_write(session)` serializes writers on SQLite so they queue in the event loop instead of timing out in the driver, and does nothing on other databases; its lock is kept per event loop, so an engine reused across `asyncio.run` calls is fine. The write functions raise `ValueError` for any database other than Postgres or SQLite. The names load on first use, like the rest of `libex_core.storage`, and raise `StorageUnavailable` if the `storage` extra is not installed.

### Known differences between the two databases
- **`lower()` matches Postgres for every character Python's Unicode tables know.** A few characters added to Unicode more recently than the Python in use may be lower-cased differently.
- **SQLite does not reject an unknown region string.** Postgres does, through its region type.

Nothing in this release opens a database or creates tables, and no command uses it.

## [0.15.0]

### Added
- **A new `libex_core.storage` subpackage holds the stored schema for books, authors, series, narrators, genres and tracks, and the links between them.** `Book`, `Author`, `Series`, `Narrator`, `Genre` and `Track` are the table classes, `Base` is their declarative base, and `UTCDateTime` and `JSONDocument` are the column types they use. Nothing in this release reads or writes a database, no command uses it, and nothing in the package creates the tables.
- **The schema works on SQLite as well as Postgres.** On Postgres it is the same tables, columns and indexes as the hosted service has, unchanged. On SQLite, timestamps come back as UTC-aware datetimes (a naive value written in is taken to be UTC), a Python `None` in a JSON column is stored as SQL `NULL` rather than the JSON value `null`, and the partial index on authors with no ASIN is created there too.
- **Importing `libex_core.storage` loads no database library.** The names above load on first access. If SQLAlchemy or aiosqlite is not installed, accessing one raises `StorageUnavailable`, a subclass of `ImportError`, whose message names the `storage` extra and the command to install it. `require_storage()` runs the same check on its own, without importing the libraries.
- **Two new install extras.** `libex-core[storage]` adds SQLAlchemy, Alembic and aiosqlite, enough for SQLite. `libex-core[postgres]` adds asyncpg on top of that. The base install is unchanged and still needs only `httpx` and `pydantic`.

## [0.14.0]

### Added
- **The package can now look Audible data up, from code and from the command line.** A new public module, `libex_core.lookup`, has ten async functions: `get_book`, `get_books`, `get_chapters`, `get_series`, `get_series_books`, `search`, `quick_search`, `abs_search`, `abs_quick_search` and `narrator_books`. Each takes the request callable (an `AudibleGet`) first, then its arguments, then `region` as a keyword defaulting to `us`, and returns the published model the hosted route returns for the same Audible answer (`BookResponse`, `BulkBookResponse`, `ChapterResponse`, `SeriesResponse`, `AbsSearchResponse`, or a list of `BookResponse`). They are the hosted live path without the cache or the database: Audible's answer is normalized and settled, and nothing is stored. Earlier entries said the command line did not fetch Audible data and had no lookup commands; it now does.
- **Audible being unreachable is reported as an outage, never answered from a stored copy and never passed off as an empty result.** Because there is no stored copy to fall back on, a lookup that could not reach Audible raises `AudibleAPIException`, and `get_books` instead lists the affected ASINs in `notFetched` when other ASINs in the same call did come back. Audible confirming it has no record is `NotFoundException` (a bulk lookup lists the ASIN in `notFound`), and is kept apart from an outage. A search or narrator lookup that matches nothing is also `NotFoundException`. The compound `Author - Series - Title` fallback in `quick_search` searches Audible only; the stored-books step the hosted service adds after it is not part of the library.
- **Bulk lookups keep the hosted accounting.** `get_books` takes up to 1000 ASINs, each entry optionally comma-separated, and rejects the whole call with `NotFoundException` (code `invalid_request`) for an empty list, more than 1000, or any value that is not an ASIN. `notFound`, `placeholderRecords` and `notFetched` return the ASINs as the caller wrote them, in request order, and no ASIN appears in more than one of them or in `books`. A record Audible sent only as a placeholder is never returned as a book: `get_book` raises `NotFoundException` with code `withheld` for it, and `get_books` lists it in `placeholderRecords`. `get_series_books` leaves out members it cannot resolve and can return an empty list.
- **Paged lookups refuse out-of-range paging instead of clamping it.** `search` and `narrator_books` raise `ValueError` for a `limit` outside 1 to 50 or a `page` outside 0 to 9. The Audiobookshelf-shaped searches return at most five matches, and for them an unknown region is `NotFoundException` with code `invalid_request` rather than `RegionException`, as on the hosted routes. Search text and ASINs the caller typed are never repeated in an exception message or a log line.
- **New `libex-core` commands: `book get`, `book bulk`, `book chapters`, `series get`, `series books`, `search`, `quick-search`, `abs search`, `abs quick-search` and `narrator books`.** Each prints the result as JSON on standard output and takes `--region`, one of the eleven regions, default `us`. `search` takes `--title`, `--author`, `--narrator`, `--publisher`, `--keywords`, `--query` and `--sort-by`; `search` and `narrator books` take `--limit` (1 to 50, default 10) and `--page` (0 to 9, default 0). `book bulk` takes ASINs as arguments, from `--file PATH` (or `-` for standard input), or both, separated by commas or white space; a file larger than 1 MiB, or one that cannot be read as text, is refused as a usage error without repeating its path. These commands need the same proxy configuration as `libex-core config` reports: with neither `LIBEX_CORE_PROXY_URL` nor `LIBEX_CORE_ALLOW_DIRECT_EGRESS` set they exit 5 and send nothing.
- **Exit statuses follow the existing contract.** A lookup that succeeds exits 0, including a bulk result that is only partly complete: it is printed whole, with the ASINs not found, withheld or not fetched listed beside the books. A rejected argument or ASIN is 2, nothing found or no chapter list is 3 (many records have no chapter list), and Audible being unreachable is 4. No status was reassigned.

### Changed
- **The generated shell completions now complete the flags and values of the leaf commands.** Bash, zsh and fish previously completed only the top-level words; they now also complete nested commands (`book get`, `abs search`, and so on), each command's own flags, and the region names after `--region`.
- **The man page now documents each nested command under its full name**, such as `book get`, and escapes the hyphens in command names so they render and search as typed.

## [0.13.0]

### Added
- **`normalize_chapters` keeps what Audible sends beyond the chapter list.** The result gains `contentReference` and `contentUrl` (Audible's `content_reference` and `content_url` groups, verbatim) and `audibleExtras`, which gathers every other key under `response`, `contentMetadata` and `chapterInfo`. Each key appears only when Audible sent something for it; `response_groups`, the echo of the request, is the one recorded omission. A chapter's own unreproduced keys ride in that chapter's `audibleExtras`. Each of these verbatim parts is sanitized and held to 64 KB and 32 levels, as a book's extras are: NUL characters are stripped (from chapter titles too), non-finite or oversized numbers become `None`, and a part over a limit is omitted and recorded. A value of an unexpected form (a `content_reference` that is a list, a `chapters` value that is not a list of objects) is carried in `audibleExtras` under its own key instead of failing validation.
- **Nested sub-chapters are normalized and kept.** A chapter's `chapters` list, which Audible nests under it, was dropped before; each sub-chapter is now normalized the same way and carried in `chapters` on its chapter, nested up to `MAX_NESTING_DEPTH` (32) levels. Sub-chapters nested deeper are kept as Audible sent them in that chapter's `audibleExtras`. Absent when Audible sent none.
- **`ChapterResponse` gains `extrasWithheld`, and `normalize_chapters` fills it.** It records what the bounding changed or left out: `sanitized` (counts), a field name (`contentReference`, `contentUrl`, `audibleExtras`) mapped to the reason that part was withheld whole, `chapterExtras` (a count per reason of chapters whose own `audibleExtras` were withheld) and `subChapters` (a count of sub-chapter lists cut at the depth limit). It is absent from the result, and `None` on the model, when nothing was withheld.
- **`libex_core.audible.extras` publishes `bound_extras(blob, asin, region)` and `MAX_NESTING_DEPTH`.** `bound_extras` sanitizes one verbatim blob and applies the size and depth caps, returning `(blob or None, counts of what was sanitized, reason it was withheld whole or None)`; book, series and chapter normalizers all use it. `MAX_NESTING_DEPTH` is 32.
- **`normalize_chapters` takes optional `asin` and `region`** (default empty strings), used only to label the log line written when something is withheld. Existing calls are unaffected.
- **`ChapterItem` gains `chapters` and `audibleExtras`, and `ChapterResponse` gains `contentReference`, `contentUrl` and `audibleExtras`.** All default to `None`, so code that builds either without them is unaffected.
- **`normalize_series` keeps every product key beyond `asin`, `title` and `publisher_summary`** as `audibleExtras`, built the way a book's is, with `extrasWithheld` recording anything left out. Both appear only when there is something to say. `SeriesResponse` gains `audibleExtras` and `extrasWithheld`, defaulting to `None`.
- **`fetch_series_search_asins(get, name, region)` searches Audible by title and returns the unique series ASINs on the matching products, in the order found.** It asks for 10 results, so it is not a complete walk. An empty list means no series was found and is not an error; the region is checked first (`RegionException`), and the name is sent to Audible as the title and is never logged or put in a raised message.

### Changed
- **The output of `normalize_chapters` and `normalize_series` is no longer limited to the previous keys.** A response that carried only the previously reproduced keys normalizes exactly as before; one that carried more now has the extra keys above. An embedder that compares the whole dictionary, or builds a model that forbids unknown fields, will see the difference.

## [0.12.0]

### Added
- **The package can now rebuild Audible's new-releases and coming-soon lists, and fetch its genre taxonomy.** A new public module, `libex_core.audible.releases`, exposes `fetch_new_releases` and `fetch_coming_soon`, both `(get, region, days=30, category=None, *, now=None)`. Audible has no endpoint for either list, so each walks the catalog sorted by release date, newest first, and keeps the books inside the window: `fetch_new_releases` the last `days` (pre-orders skipped), returned newest first; `fetch_coming_soon` the next `days` (titles already out skipped), returned soonest first. Books with no parseable release date are skipped by the walk and never returned. `now` defaults to the current UTC time and can be passed to fix the window. The request callable is the first argument and the region is checked first (`RegionException` for one of the eleven it is not).
- **Lower-level pieces of the same walk are public too.** `walk_catalog(get, region, category_id, collect, should_stop)` runs one catalog query with your own date gates and returns the accepted books deduped by ASIN. `new_releases_gates` and `coming_soon_gates` build the two gates for a window, `new_releases_sort_key` and `coming_soon_sort_key` give the orderings, and `release_datetime` reads a normalized book's `releaseDate` back into a datetime, or `None`. The constants `CATEGORIES_PATH`, `RELEASE_PAGE_SIZE` (50) and `GENRE_TAXONOMY_LEVELS` (5) are exported with them.
- **`fetch_catalog_genres(get, region)` fetches one region's genre taxonomy, flattened, and `build_category_tree` shapes it.** `flatten_genre_nodes` turns the response into one row per node per parent (`genre_id`, `name`, `parent_id`, with `""` for a top-level node), following the tree to whatever depth Audible returned and deduping on node and parent. `build_category_tree(nodes, *, flat=False, depth=None)` returns those rows as a nested tree, or with `flat=True` as a flat list in which every node carries its ancestors root-first; both are sorted by name at every level, and `depth` limits the levels returned (1 is the top level only).
- **`CategoryNode`, `CategoryAncestor` and `FlatCategoryNode` are now public in `libex_core.models`.** They are the shapes `build_category_tree` returns. Field names, optionality and defaults are unchanged from the hosted service's copies.
- **A window is as complete as the walk can make it, and no more.** Audible caps every catalog query at roughly 535 results however it is filtered, and a parent category is not a superset of its children. Called with no `category`, the functions return a slice of the catalog, not the full window; reaching all of it means calling once per category id from the taxonomy and merging, which this module does not do for you.
- **The books come back as normalized, not settled.** Their tri-state flags are left as Audible gave them, for the caller to store or settle. Nothing here reads or writes a cache or a database, and the module does not log.
- **`fetch_new_releases` and `fetch_coming_soon` raise `ValueError` for `days` below 1, and `build_category_tree` for a `depth` below 1.** The messages repeat nothing the caller passed in. A `NotFoundException` or `AudibleAPIException` from the request callable propagates unchanged.

## [0.11.0]

### Added
- **The package can now look authors up on Audible.** A new public package, `libex_core.audible.authors`, has four modules. `profile` has `fetch_author_profile`, which returns an author's contributor record raw; `normalize_author`, which turns one into the author response shape; and `fetch_author_suggestion_asins`, which asks Audible's search suggestions which authors a name resolves to and returns their ASINs in Audible's order, unvalidated. `screens` has `fetch_author_books_by_screen` and its `ScreenBooksResult`: the author's books read from Audible's author-detail screen, the one source that names the author by ASIN. `catalog` has `fetch_author_books_by_catalog` and its `CatalogBooksResult`: the windowed, category-sliced catalog search, with each book attributed to the author by ASIN. `by_name` has `walk_author_books_by_name`, for a caller that has a name and no ASIN, with `NameWalkOutcome` and the `STOP_COMPLETED`, `STOP_PLATEAU`, `STOP_PAGE_CAP`, `STOP_PAGE_FAILED` and `STOP_DEADLINE` reasons. Like the other fetch functions, each takes the request callable as its first argument and checks the region first (`RegionException` for one of the eleven it is not), before anything is sent.
- **Each author-books walk reports what it found and whether it reached a confirmed end, and leaves the rest to the caller.** None of the three is complete alone, and none fills a gap from another: a walk that stopped early, hit a cap or lost a page says so in its result and returns the ASINs it had. Nothing here retries, falls back to a different source or decides that a short answer is good enough. The walks are bounded (page, result and time limits, and an optional absolute `deadline`); a result that stopped on one of those limits is not a complete one.
- **`AuthorResponse` is now public in `libex_core.models`.** Field names, optionality and defaults are those of the hosted service's copy: `id`, `asin`, `name`, `description`, `image`, `region`, `regions`, `genres` and `updatedAt`.
- **`fetch_author_profile` raises `ValueError` for a value that is not an ASIN, and `fetch_author_books_by_screen` answers one with an empty, clean result.** In both cases nothing is sent, and the `ValueError` message never repeats the rejected value. The screen walk returns no ASINs, no pages fetched and a completed reason rather than raising, because Audible answers an unknown author ASIN the same way. `fetch_author_books_by_catalog` does not validate its author ASIN, since it is only compared with what comes back and never sent.

## [0.10.0]

### Added
- **The package can now filter and sort a list of book dictionaries.** A new public module, `libex_core.shaping`, exposes `filter_dicts` and `sort_dicts`, the in-memory filtering and sorting the live book lists use, with no database or web framework involved. `filter_dicts(items, filters)` takes a dictionary of filter name to value, ignores `None` values and unknown names, keeps the input order, and returns the input list itself when no filter is active. `sort_dicts(items, sort, order, allowed)` returns the list unchanged when `sort` is empty or not in `allowed`; otherwise it sorts ascending, or descending when `order` is `desc`, and books missing the field or holding `None` for it go to the end in either direction. `allowed` is any container of field names, such as a tuple or the keys of a dictionary; only membership is tested.
- **The filter and sort surface is published as data, so a front end can build its parameters from it.** `BOOK_FILTER_SPECS` is a tuple of `FilterSpec` entries (`name`, `type`, `description`) for the twelve filters: `language`, `book_format`, `explicit`, `whisper_sync`, `has_pdf`, `is_vvab`, `plan_name`, `rating_better_than`, `rating_worse_than`, `longer_than`, `shorter_than` and `genre`. `BOOK_FILTER_FIELDS` is the set of their names. `BOOK_SORT_FIELDS` is the tuple of sortable book fields (`title`, `releaseDate`, `rating`, `lengthMinutes`, `language`, `publisher`, `updatedAt`), `BookSortField` is an enum with exactly those members, and `SortOrder` is the `asc`/`desc` enum.
- **Filters are limited to what is cheap on a list already in memory.** Numeric ranges, equality on the format and boolean fields, plan membership, and a case-insensitive partial match on genre names. Free-text search on title or description is not offered. A book missing the field a range filter targets is excluded by that filter, so a book with no length is never "longer than" anything.

## [0.9.0]

### Added
- **The package can now search Audible, not only fetch by ASIN.** A new public module, `libex_core.audible.search`, exposes `build_search_params`, `fetch_search_products` and `fetch_suggestion_asins`, plus the constants `SEARCH_PATH`, `SEARCH_SUGGESTIONS_PATH` and `MAX_SEARCH_RESULTS` (50). `build_search_params` takes the filters (`title`, `author`, `keywords`, `narrator`, `publisher`, `products_sort_by`) and `limit` and `page`, keyword-only, and returns the parameters of a catalog search; a filter that is missing or an empty string is left out, and no filter at all is allowed. `fetch_search_products` sends them and returns the matched products raw, with the response groups and image sizes added so full product metadata comes back in one call. `fetch_suggestion_asins` asks Audible's search suggestions what a partial query resolves to and returns the ASINs of the book rows in the order Audible gave them, unvalidated. As with the other fetch functions, the request callable is the first argument, and the region is checked first (`RegionException` for one of the eleven it is not).
- **`build_search_params` raises `ValueError` for a `limit` outside 1 to `MAX_SEARCH_RESULTS` or a negative `page`.** The message names the bounds and never repeats any search text; the package neither inspects nor logs search text. Earlier hosted behaviour silently clamped an over-large `limit` to 50; the package does not clamp, it refuses. Audible stops returning results past page 9, and that is not enforced here: a `page` above 9 is accepted and sent.
- **The Audiobookshelf custom-metadata-provider models are now public in `libex_core.models`.** `AbsSeriesRef`, `AbsBookResponse` and `AbsSearchResponse` describe that format, which is deliberately narrower than the AudiMeta-derived shapes; the module docstring now says they are not derived from AudiMeta. `to_abs_book` converts a normalized book dictionary into an `AbsBookResponse`. Field names, optionality and defaults are unchanged from the hosted service's copy.

## [0.8.0]

### Added
- **`BulkBookResponse.notFetched`, a list of the requested ASINs that could not be looked up because Audible was unreachable and no stored or cached copy covered them.** It defaults to an empty list, so code that builds a `BulkBookResponse` without it is unaffected, and it serializes always-present like `placeholderRecords`.

### Changed
- **The documented meaning of `BulkBookResponse.notFound` narrows to ASINs Audible confirmed it has no record of.** The field, its type and its position are unchanged, but it no longer holds ASINs that could not be fetched; those belong in `notFetched`. An embedder that fills `notFound` itself and treated it as "missing or unfetched" should move the unfetched ones to `notFetched`. The field description (visible in the generated schema) was reworded to match.

## [0.7.0]

### Added
- **The package now ships a `libex-core` command line.** It is installed as the `libex-core` script and also runs as `python -m libex_core`. Two commands exist so far: `libex-core config` prints, as JSON, whether requests would go through a proxy or leave directly, and the proxy host (never the proxy URL, and no request is made); `libex-core completion bash|zsh|fish` prints a completion script. Neither fetches Audible data. `--help` and `--version` work everywhere. By default the library's warnings print to standard error with no flag, `-q` prints only the final error line, `-v` asks for more detail (the package emits no INFO-level records, so it adds nothing), and `-vv` adds debug output with tracebacks. Results go to standard output; logs and errors go to standard error.
- **The command line's exit status says what kind of failure it was.** 0 success, 1 unexpected error, 2 bad usage or a rejected argument, 3 not found, 4 Audible unavailable, 5 bad environment configuration, 130 interrupted, 141 the reader of standard output went away. A `LibexException` maps by its `code`: `invalid_request` is 2, `not_on_audible`, `not_in_libex` and `withheld` are 3, `upstream_unavailable` is 4, and a code that has no mapping is 1. The error line on standard error ends with the code; `config_error` and `unexpected_error` are the command line's own and are never raised by the library. These numbers are a public contract and will not be reassigned.
- **The command line reads two environment variables.** `LIBEX_CORE_PROXY_URL` is the http or https proxy every request goes through; it is an environment variable and not an option because it can carry credentials. `LIBEX_CORE_ALLOW_DIRECT_EGRESS` (`1`, `true`, `yes` or `on`; `0`, `false`, `no`, `off` or empty to refuse) lets requests leave from the machine's own address when no proxy is set. Any other value is a configuration error (exit 5), reported even when a proxy is set.
- **A man page and shell completions install with the wheel**, under the environment prefix.
- **The package is now buildable and publishable as `libex-core`.** It declares `httpx` (`>=0.28.1,<0.29`) and `pydantic` (`>=2.13.4,<3`) as its dependencies, requires Python 3.12 or later, and ships a `py.typed` marker so type checkers use its annotations.

### Changed
- **The library still reads no environment variable.** The command line's environment module is the only code in the package that touches the process environment, and only when a configuration is requested, never at import. An embedder's own environment cannot change what the library does. The package docstring now states this.

## [0.6.0]

### Added
- **The package can now fetch and normalize Audible books, chapters and series, not only describe them.** New public modules: `libex_core.audible.books` (`fetch_products`, `normalize_product`, `normalize_products`, `settle_flags`, `settle_flags_list`, `filter_products`, `is_placeholder_record`, and the response-group and image-size constants), `libex_core.audible.extras` (`build_extras`), `libex_core.audible.chapters` (`fetch_chapter_metadata`, `has_chapter_info`, `normalize_chapters`, `CHAPTERS_RESPONSE_GROUPS`) and `libex_core.audible.series` (`fetch_series`, `fetch_series_book_asins`, `normalize_series`). Normalized output is the same, key for key, as hosted Libex produced before the move; it is pinned by golden files. Earlier entries said this package did not fetch or normalize a product; that no longer holds.
- **Every fetch function takes the request callable as its first argument.** `AudibleGet`, a new `Protocol` in `libex_core.audible.client`, describes it, so how a request leaves the process stays the embedder's choice. The fetch functions check the region first (`RegionException` for one of the eleven it is not) and every ASIN (`ValueError`, with a message that never repeats the rejected value) before anything is sent. `validated_asin` is exposed for the same check; it accepts a lowercase ASIN and returns it uppercased, which is also the form the fetch functions send. Hosted Libex screens out values that are not ASINs before it calls `fetch_products`, so one bad value does not reject the rest of a batch; the core itself still raises `ValueError` for the whole call. `fetch_products` accepts at most 50 ASINs per call; it does not split a longer list for you.
- **`libex_core.log_safety`** exposes `is_safe_log_value`, `safe_asin_for_log` and `window_elapsed`, the checks the package uses to keep caller- or Audible-supplied text out of log lines and to rate-limit repeated warnings. `is_safe_log_value` is the same rule hosted Libex used; it is no longer importable from hosted Libex's own logging module.

### Changed
- **The transport's concurrency and retry internals moved into private modules.** Nothing public was removed: `author_books_concurrency` is still importable from `libex_core.audible.client`. Code that reached into the transport's other internals, such as its semaphore or its concurrency limit, now finds them in `libex_core.audible._concurrency`, a private module with no stability promise.
- **The malformed-author-ASIN warning redacts unsafe values.** It used to log the book's ASIN, the malformed author ASIN and the author name verbatim. The book's ASIN is now `REDACTED` unless it is a well-formed ASIN; the malformed author ASIN and the author name are logged as-is only if `is_safe_log_value` accepts them (short catalogue text), otherwise `REDACTED`. Other warnings naming an ASIN from Audible use the same well-formed-ASIN rule.

## [0.5.0]

### Added
- **New public `ErrorCode` enum, and every `LibexException` gains a `code` attribute.** `ErrorCode` is a `StrEnum` with `NOT_IN_LIBEX`, `NOT_ON_AUDIBLE`, `WITHHELD`, `UPSTREAM_UNAVAILABLE` and `INVALID_REQUEST`, its values being the lowercase names. Each exception class has a default: `NotFoundException` is `NOT_ON_AUDIBLE`, `RegionException` is `INVALID_REQUEST`, and `AudibleAPIException`, `CacheException` and the base `LibexException` are `UPSTREAM_UNAVAILABLE`. A raise site can override it with a new optional `code=` argument. Existing raises keep working untouched and keep their messages and status codes; the argument is keyword-optional and last, so no positional call changes meaning.

## [0.4.1]

### Changed
- **The package and its response models are no longer described as a drop-in AudiMeta replacement.** The shapes are derived from AudiMeta's and differ from them in places; the package docstring and the models module now say so. Documentation only: no model, field, default or function changed.

## [0.4.0]

### Added
- **`BulkBookResponse` gains `placeholderRecords: list[str]`, defaulting to an empty list.** Existing construction sites keep working untouched. What moves for every embedder is the serialized shape: a dump of `BulkBookResponse` now carries a `placeholderRecords` key, `[]` unless something supplied it, so a golden file or snapshot compared against a dump will differ on upgrade. That is why this is a MINOR rather than a PATCH. The field means requested ASINs for which Audible returned a record carrying its 2200-01-01 placeholder publication date, deliberately left out of `books` and never also in `notFound`. Nothing in this package fills it; the embedder decides what goes in.

### Changed
- **The `notFound` field description on `BulkBookResponse` is reworded; its type and default are unchanged.** It now points placeholder records at `placeholderRecords`, and says the list can also hold ASINs that could not be fetched in this request and had no stored copy. This changes the schema's description text in generated OpenAPI, nothing else.

## [0.3.0]

### Added
- **`BookResponse` gains eight optional fields: `numRatings`, `numReviews`,
  `publicationName`, `publicationDatetime`, `extendedProductDescription`,
  `productState`, `audibleExtras` and `extrasWithheld`.** Every one defaults
  to `None`, so existing construction sites keep working untouched. What
  does move for every embedder is the serialized shape: pydantic emits all
  eight by default, so a dump of `BookResponse` — and of
  `BulkBookResponse`, which contains it — now carries eight more keys,
  `null` unless something supplied them. A golden file or snapshot compared
  against a dump will differ on upgrade, which is the whole reason this is
  a MINOR rather than a PATCH.

- **Nothing in this package fills them.** `libex_core` defines the response
  shape; it does not fetch an Audible product or normalize one into this
  model. An embedder constructing a `BookResponse` decides what goes in
  these fields, so the contracts below are what the shape *means*, not
  behaviour the package performs. `numRatings` and `numReviews` are the
  counts behind the `rating` average, which on its own cannot distinguish
  4.8 from three ratings from 4.8 from two hundred thousand.
  `publicationName` and `publicationDatetime` place a periodical or podcast
  episode in its publication; the datetime is a full instant with a literal
  trailing `Z`, distinct from `releaseDate`, which is a bare calendar date.
  `extendedProductDescription` is the long-form description with its markup
  intact — `description` and `summary` are the flattened ones — and is
  upstream HTML, neither validated nor rewritten here, so encode it on
  output. `productState` is Audible's own state string; match it as an
  opaque string rather than modelling it as an enum, because the vocabulary
  is Audible's and can grow without notice.

- **`audibleExtras` is defined as a verbatim catch-all, and three details of
  that definition bind anyone who populates or reads it.** It holds every
  top-level key of Audible's product response that the named fields do not
  already reproduce, so a key Audible invents later surfaces on its own
  rather than disappearing between the fetch and the response. Verbatim
  means value for value, not byte for byte: the content is parsed JSON, so
  key order is not preserved, duplicate keys are already collapsed, and
  numeric spelling is normalized (`1e3` arrives as `1000.0`). Nothing in it
  is ever hoisted to the top level — no splat, no key set at runtime —
  which is what makes an upstream key named `asin` structurally unable to
  collide with the first-class field of that name; it stays nested and is
  read there. And it is tri-state: `null` means nothing was captured, `{}`
  means the product carried nothing beyond the named fields, and an object
  is content. Treat every value in it as untrusted input, including the
  URLs it will contain.

- **`extrasWithheld` is the record of what was left out of `audibleExtras`
  and why, so an omission is stated rather than inferred.** It sits beside
  the blob rather than inside it deliberately: the blob is documented as
  Audible's keys only, and a key invented here would collide with a real
  upstream one the day Audible ships a field by that name. Unlike
  `audibleExtras` it is not tri-state — `None` means nothing was withheld,
  and draws no distinction between an intact blob and no blob at all.

## [0.2.0]

### Security
- **`get_audible_url()` now rejects a `.` or `..` path segment anywhere
  between the path's slashes, not only a dot segment at the very front.** The
  existing guard only inspected the path's leading character, so a path like
  `/1.0/catalog/products/../../internal` passed it untouched; httpx then
  applies ordinary RFC 3986 dot-segment removal while parsing the URL this
  function builds, silently collapsing that path to `/1.0/internal` — an
  endpoint this function never intended to address. The guard that checks the
  finished URL's host, scheme and port could not catch it either, because
  none of those three change when only the path collapses. A segment that is
  `.` or `..`, one whose percent-encoding (`%2e`/`%2E`) decodes to either of
  those, or one containing an encoded separator — `%2f`/`%2F` for `/`, and
  now `%5c`/`%5C` for the backslash the guard already rejected in its literal
  form — raises `ValueError`. This is a breaking change for a caller of
  `LibexClient.get()` that was constructing a path containing one of these;
  no call site in this codebase ever has, every one interpolating a single
  bare id. The encoded spellings rest on weaker ground than the literal one
  and are refused anyway: httpx never turns an encoded separator or an
  encoded dot into a live one before the request leaves this process, so
  those forms are rejected for what a server might do with them after
  decoding, which is not something this library can see or verify.
- **A path containing `?` or `#` is now rejected outright, whether or not a
  dot segment is visible in it.** This is the change most likely to reach an
  existing caller: code that inlined a query string into the `path` argument,
  rather than passing `get()`'s own `params`, now raises `ValueError` where it
  previously made a request. The reasoning is not the one above. Neither
  character belongs to the path component — each one ends it — so whatever
  sits immediately in front of one is the path's real final segment, which is
  somewhere a check that splits on `/` never looks. When that segment was `.`
  or `..`, the collapse had already happened in the bytes leaving this
  process: `/1.0/catalog/products/..?x` was transmitted with a path of
  `/1.0/catalog?x`, a level above the prefix the caller asked for, and
  `/1.0/catalog/products/.#x` as `/1.0/catalog/products`, the fragment never
  reaching a server at all. That is measured behaviour of this library, not an
  assumption about anybody else's. Refusing the two characters removes the
  whole shape rather than the individual spellings of it, and costs nothing
  legitimate: query parameters belong in `get()`'s `params` argument, which
  httpx appends to the URL this function returns, and a fragment is never sent
  to a server in the first place.
- **What this guard does not cover, deliberately.** It compares segments
  against fixed spellings rather than decoding them, so double- and
  nested-encoded forms (`%252e%252e`), unicode normalization, and overlong
  encodings such as `%c0%af` are still accepted, and no claim is made that
  they are caught. None of them decode into a separator in httpx before a
  request leaves this process, nothing in this package produces one, and
  matching every possible spelling of a dot is an arms race with no end. Read
  the guard as closing the shapes named above, not as general input
  sanitisation.

## [0.1.0]

First tracked version of `libex_core`, and the first entry in this file.
Everything below shipped in the same change that started this line, which is
why the line opens with a break rather than growing into one later.

### Removed
- **The module-level `configure_transport()` and `audible_get()` functions,
  and the process-wide transport state behind them, are gone.** Egress used
  to be a single, mutable choice for the whole process: call
  `configure_transport()` once at import, and every subsequent call to the
  module-level `audible_get()` read back whatever it had set. There is no
  compatibility shim for either name — code built against them breaks
  immediately on upgrade rather than continuing to work with a deprecation
  warning.

### Added
- **`LibexClient` replaces both: one instance per caller, whose transport is
  decided once at construction and never replaced for that instance's whole
  life.** Construct one with `LibexClient(proxy_url=..., allow_direct_egress=...)`
  and call its `get(region, path, ...)` method wherever code used to call the
  module-level `audible_get()`. `proxy_url` has no default — omitting it is a
  `TypeError` from Python's own argument checking, not a state this library
  has to notice and refuse at request time, so "nobody ever decided how this
  instance egresses" is not a state a `LibexClient` can be in. A blank or
  missing `proxy_url` still needs a separate `allow_direct_egress=True` to
  mean "leave on this machine's own address" — otherwise it's refused,
  because an empty proxy setting is indistinguishable, at the wire, from one
  simply left unset by mistake. That guard carries over unchanged from
  `configure_transport()`; only its scope moves from once-per-process to
  once-per-instance.
- **An instance can be closed permanently**, with `await client.aclose()` or
  by using it as `async with LibexClient(...) as client:`. Closing is
  terminal: every `get()` call afterward raises `RuntimeError` instead of
  silently rebuilding a client and egressing again, and a second `aclose()`
  is a no-op. `is_open` reports whether a live HTTP client currently exists
  for the instance — it reads `False` both before the first request and
  after `aclose()`, deliberately not the complement of the underlying HTTP
  library's own closed-state check.
- Concurrency, retry and backoff limits stay process-wide rather than
  becoming per-instance state: every `LibexClient` built in the same process
  still shares one exit IP and draws from the same two concurrency pools,
  regardless of how many instances exist.

### Security
- **A crafted request path could have redirected a request to a host other
  than Audible's own.** The URL builder concatenated a caller-supplied path
  directly onto the Audible hostname with no separator guarantee, so a path
  starting with `@` could turn the rest of the string into userinfo and push
  the intended host aside — a path like `"@evil.example/x"` resolved to
  `evil.example`, not Audible. A path is now rejected outright unless it
  starts with a single `/`, contains no backslash, and the finished URL
  still resolves to exactly the intended host, scheme, and port. This starts
  to matter only from this release on, because `get()` is the first published
  method that takes a path directly from whoever is embedding the library —
  every caller of the equivalent internal function before it always passed a
  fixed path, so the bug existed but had no way to be reached.
