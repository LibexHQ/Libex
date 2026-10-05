# libex-core

The Audible metadata library and `libex-core` command line behind
[Libex](https://github.com/LibexHQ/Libex). It runs on your own machine,
fetches from Audible, and returns the same JSON shapes the Libex API
publishes: books, chapters, series, authors, narrators, search and new
releases, in all eleven Audible marketplaces (`us uk ca au de fr it es jp in br`).

Libex also runs as a hosted API, and a free public instance is at
[libexdb.com](https://libexdb.com). You do not need it to use this package.

`pip install libex` is a different, unrelated project. This one is
`libex-core`.

## Install

```
pip install libex-core
pip install "libex-core[storage]"    # adds the optional local store (SQLite)
pip install "libex-core[postgres]"   # the store, with Postgres as well
pip install "libex-core[socks]"      # lets the proxy be a SOCKS5 proxy
```

Python 3.12 or newer. The base install depends on `httpx` and `pydantic`
only. The `libex-core` command is installed with the package, extras or not.

## Set up the network first

Every request goes to Audible from this machine. Audible sees the address it
comes from together with what you look up, which is part of your reading
history tied to your connection, so the package makes you decide how to
connect before it sends anything. The command line reads two environment
variables, never an option, because a proxy URL can carry credentials that
other processes could read from a command line:

| Variable | Meaning |
|---|---|
| `LIBEX_CORE_PROXY_URL` | An `http://`, `https://`, `socks5://` or `socks5h://` proxy every request goes through. May carry credentials. A SOCKS5 URL needs its port and the `socks` extra. |
| `LIBEX_CORE_ALLOW_DIRECT_EGRESS` | `1`, `true`, `yes` or `on` lets requests leave from this machine's own address when no proxy is set. Anything else, or unset, refuses. |

```
export LIBEX_CORE_PROXY_URL="http://user:password@proxy.example:8080"
libex-core config        # {"transport":{"mode":"proxy","host":"proxy.example"}}

# or, knowing what it means:
export LIBEX_CORE_ALLOW_DIRECT_EGRESS=1
```

SOCKS5 proxies need `pip install "libex-core[socks]"`; without it a SOCKS5
URL is refused when the client is built, not on the first request. `socks5://`
and `socks5h://` behave the same: the proxy is always handed Audible's
hostname and resolves it, and TLS to Audible stays end to end through the
tunnel. A username and password in a SOCKS5 URL are sent to the proxy as
RFC 1929 username/password authentication, which is cleartext between this
machine and the proxy; only the tunnelled TLS to Audible is encrypted. Use
SOCKS5 credentials only on a network you trust, such as a local or private
one. `socks4://` and `socks4a://` are not supported.

With neither set, a command that needs Audible exits with status 5 and says
what to set. `libex-core config` makes no request and never prints the proxy
URL. What Audible and a proxy can see is described in the
[privacy notice](https://github.com/LibexHQ/Libex/blob/main/PRIVACY.md#the-embeddable-library).

## Command line

Results are JSON on standard output; logs and errors go to standard error.
`--region` defaults to `us`. Book ASINs belong to one marketplace, so use the
region the book was published in.

```
libex-core book get ASIN --region us
libex-core book bulk ASIN1 ASIN2 --sort rating --order desc
libex-core book bulk --file asins.txt --language english
libex-core book chapters ASIN

libex-core series get SERIES_ASIN
libex-core series books SERIES_ASIN
libex-core series search "Mistborn"

libex-core author get B001IGFHW6
libex-core author search "Sanderson"
libex-core author books B001IGFHW6 --sort releaseDate
libex-core author books-by-name "Brandon Sanderson"

libex-core narrator books "Michael Kramer"

libex-core search --title "Hobbit" --author "Tolkien" --limit 5
libex-core quick-search "project hail mary"
libex-core abs search --title "Hobbit"
libex-core abs quick-search --keywords "project hail mary"

libex-core releases new --days 30
libex-core releases coming-soon --days 90 --category ID
libex-core releases categories --flat
```

`libex-core COMMAND --help` lists every option, including the filters
(`--language`, `--genre`, `--longer-than` and others) and `--sort` / `--order`
that the list commands share. `libex-core completion bash` (also `zsh`,
`fish`) prints a completion script.

When a list of books may not be whole, it is still printed, a notice naming
why goes to standard error, and the status is 0.

### Exit status

| Status | Meaning |
|---|---|
| 0 | Succeeded. |
| 1 | Unexpected error. Rerun with `-vv` for a traceback. |
| 2 | The command line was not understood, or an argument was rejected. |
| 3 | The requested item does not exist, or nothing matched. |
| 4 | Audible could not be reached or did not answer. Retrying later may succeed. |
| 5 | Configuration is missing or invalid, or the local store is off, missing its extra, unreachable or not ready. |
| 130 | Interrupted. |
| 141 | The reader of standard output went away. |

## Local store

By default nothing is kept. Setting `LIBEX_CORE_STORAGE` turns on a local
database that lookups write what Audible answered into, under the same merge
rules as the hosted service, and that serves the stored copy when Audible is
unreachable. It needs the
`storage` extra.

| `LIBEX_CORE_STORAGE` | Store |
|---|---|
| unset, empty or `off` | none (default) |
| `sqlite` | `libex.db` in your user data directory: `~/.local/share/libex-core/` on Linux (or under `$XDG_DATA_HOME`), `~/Library/Application Support/libex-core/` on macOS, `%LOCALAPPDATA%\libex-core\` on Windows |
| an absolute path | that SQLite file |
| a `sqlite:///` or `postgresql://` URL | that database (`postgres` extra for Postgres) |

```
export LIBEX_CORE_STORAGE=sqlite
libex-core db upgrade     # creates the file; the only command that changes the schema
libex-core db status      # exits 0 only when the store is ready
libex-core book get ASIN   # now also stored
libex-core db book ASIN    # read back from the store, no request to Audible
```

The other `db` commands read what is stored without contacting Audible:
`books`, `chapters`, `author`, `author-books`, `series`, `series-books`,
`narrators`, `narrator-books`, `genres`, `plans`, `plan`, `vvab`,
`new-releases`, `coming-soon` and `stats`. Each prints the shape of the hosted
API's `/db` route of the same name and exits 3 when it finds nothing. A book
or series is stored once per marketplace: `db book`, `db chapters` and
`db series` print the record stored first, and `--region` picks a
marketplace's own (exit 3 if it has none). The store is never upgraded implicitly, and a database that holds tables this
package did not create is refused untouched.

**The store is a plaintext record of what you have looked up.** On Linux and
macOS a new SQLite file is created readable by you only. The storage setting
can carry database credentials, so it is read from the environment, never
accepted as an option, and never printed. To delete a SQLite store, remove
the file and its `-wal` and `-shm` companions beside it.

### Reading the store directly

`libex_core.lookup` also reads a store without asking Audible:
`stored_book`, `stored_books`, `stored_chapters`, `stored_series`,
`stored_author` and `chapters_confirmed_at`. Each takes the `LocalStore` first and returns the same model
the live lookup returns, wrapped in `Stored`.

```python
from libex_core.lookup import stored_book

found = await stored_book(store, asin, region="us")
if found is not None:
    print(found.value.title, found.confirmed_at)
```

- `region` is required, with no default; a record stored for another
  marketplace is never returned.
- `Stored.confirmed_at` is when Audible last confirmed the record. `None`
  means it has not been confirmed since 0.22.0 (for instance, it was stored
  earlier or only mentioned by another record), not that it is stale.
- A record that is not stored returns `None`; `stored_books` leaves it out.
- A closed store raises `StoreClosed`.
- `stored_chapters` returns `None` when the stored chapters answer is empty.
  To tell that from a book never asked about, call
  `chapters_confirmed_at(store, asin, region=...)`: it returns when Audible
  last answered for the book's chapters, empty answers included, or `None`
  if it never has. A date with no `stored_chapters` result means Audible
  confirmed the book has none.

## Use it as a library

```python
import asyncio

from libex_core.audible.client import LibexClient
from libex_core.exceptions import LibexException
from libex_core.lookup import get_book, search


async def main() -> None:
    # The proxy decision is explicit and has no default: pass a proxy URL,
    # or allow_direct_egress=True to send from this machine's own address.
    asin = "..."  # the book's ASIN
    async with LibexClient(proxy_url="http://proxy.example:8080") as client:
        try:
            book = await get_book(client.get, asin, region="us")
            print(book.title)
            for hit in await search(client.get, title="Hobbit", limit=5):
                print(hit.asin, hit.title)
        except LibexException as exc:
            print(exc.code, exc.message)


asyncio.run(main())
```

The library reads no environment variable; every setting is an argument.
Each lookup in `libex_core.lookup` takes the request callable (`client.get`)
first and `region` as a keyword, and returns a model from `libex_core.models`
(pydantic). Pass `store=` a `libex_core.storage.LocalStore` to use the local
store as above; without it nothing is persisted. Importing `libex_core`
itself imports nothing else, and the storage libraries load only when
`libex_core.storage` is used.

### Supplying your own connection

`LocalStore(url)` makes the database connection itself. If you need one it
cannot make, such as a tunnel, a short-lived token or a custom connection
class, pass a keyword-only `connect=` hook and make it yourself:

```python
store = LocalStore("postgresql+asyncpg://", connect=my_asyncpg_connect)  # () -> awaitable asyncpg.Connection
store = LocalStore("sqlite+aiosqlite:///libex.db", connect=my_open)      # (path) -> sqlite3.Connection
```

With a Postgres hook the URL must be bare, `postgresql+asyncpg://`: a host,
user, password or option in it is refused. A SQLite hook is called with the
path libex-core has already checked (a symbolic link is refused, and on Linux
and macOS a new file is created readable by you only) and is an ordinary
function returning a DB-API connection: `sqlite3`, or a build with the same
interface such as SQLCipher (`sqlcipher3`), with the key applied inside the
hook. libex-core wraps the connection itself and reads it once before use, so a
wrong key surfaces as `StoreConnectionError` with the connection already
closed. Write-ahead logging is
not set beforehand: `upgrade()` switches the file to it later, over the
connections you supply. Schema checks, refusal of a database this package did not create, upgrades and write
locking all apply to the connections you supply, and a hook that raises or
returns the wrong type surfaces as `StoreConnectionError` naming only the
exception class; a connection of the wrong type is closed first.
`store.connection_mode` is `"managed"` or `"caller"`.

With a hook, these become your responsibility, and libex-core can neither
manage nor check them:

- Postgres TLS mode and certificate verification.
- Keeping `PG*` variables and `~/.pgpass` from being read, which asyncpg does for
  anything you leave out.
- Never resending a password in plain text after a failed encrypted attempt.
- `gsslib`, `krbsrvname` and `server_settings`.
- Whether `SSLKEYLOGFILE`, if set, makes the driver write the connection's encryption
  keys to a file.

## Links

- Source and issues: <https://github.com/LibexHQ/Libex>
- Changelog: <https://github.com/LibexHQ/Libex/blob/main/libex_core/CHANGELOG.md>
- Privacy: <https://github.com/LibexHQ/Libex/blob/main/PRIVACY.md>
- Hosted API: <https://libexdb.com>
- License: MIT
