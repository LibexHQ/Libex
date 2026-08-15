# Privacy

This is the privacy policy for the **public Libex instance** at
[libexdb.com](https://libexdb.com), and for the old address
`libex.lostcartographer.xyz` for as long as it keeps serving (until
4 November 2026).

If you run your own copy of Libex, most of this does not apply to you — see
[Self-hosting](#self-hosting) at the end.

Libex has no accounts, no logins, no API keys and no cookies. Nothing here
describes a profile of you, because there isn't one to describe. What this
document does describe is the request logging the public instance keeps, who
else can see it, and what I can and can't do if you ask me to remove it.

I've written this to be accurate rather than reassuring. Where the honest
answer is "I can't do that," it says so.

> **A note on what this is.** I'm not a lawyer, and this is a description of
> how the software actually behaves, not legal advice and not a set of
> borrowed clauses. Everything in the "What gets recorded" section can be
> checked against the source — it's all in `app/core/middleware.py` and
> `app/core/logging.py`. Where something is a setting in a third party's
> console rather than a line of code, I've said so instead of pretending
> otherwise.

---

## Who runs this

The public instance is run by the maintainer of Libex
(GitHub: [@SunBroLynk](https://github.com/SunBroLynk)) as a free service. "I"
throughout this document means that person. Libex is open source and has other
contributors, but they don't run the public instance and they don't have
access to its logs.

Libex is hosted on infrastructure operated within LibexHQ. That is not a
separate company processing your data on my behalf — it is part of the same
project, run by the same people, under the same rules as everything else in
this document.

**Contact.** The fastest route is a
[GitHub issue](https://github.com/LibexHQ/Libex/issues). For anything you'd
rather not raise in public, the email published in
[`SECURITY.md`](SECURITY.md) reaches me.

---

## The short version

On every request that isn't `/health`, the public instance writes one log line
containing: the method, the path, the response status, how long it took, your
user agent, the host header you used, and the *names* of the query parameters
you sent. Parameter values are allowlisted — structural options like `region`
and `limit` keep their values, anything you typed is replaced with `REDACTED`,
and a parameter name Libex doesn't recognise is thrown away rather than
written down.

**No IP address. Nothing you searched for. No cookie, no identifier, nothing
that links one of your requests to another.**

Those lines go to the container's stdout — warnings and errors to stderr — to
a rotating file on the server, and to **Axiom**, a third-party log service.
**Cloudflare** sits in front of the public instance, terminates TLS, and keeps
its own logs — including your real address — which I don't control and can't
reach.

The point of logging at all is to see which endpoints are failing and how
slow they are, so a bad release doesn't break things quietly. None of that
needs to know who you are.

---

## What gets recorded on every request

One log line per request, built in `LoggingMiddleware.dispatch`:

| Field | What it actually contains | Why it's there |
|---|---|---|
| `userAgent` | Your `User-Agent` header verbatim, e.g. `Audiobookshelf/2.x` or `python-httpx/0.27`. | Tells me which client software is calling. This is how I know who a change will break, and it's what I used to check that real consumers still worked when the public instance moved to a new hostname. |
| `method` | `GET`. | Completeness. |
| `url` | The path, and nothing after it — e.g. `/author/books`, or `/book/B01234567` where the book's identifier is part of the path itself. A path matching no route at all is still recorded as you sent it. | Which endpoints are used and which are failing. |
| `query` | The names of the query parameters you sent, with **values allowlisted** — structural params keep their values, anything you typed is replaced with `REDACTED`, and a name Libex doesn't recognise is dropped and counted rather than written down. See [below](#what-you-type-is-not-recorded-either). | The path alone can't tell me which parameters consumers actually use, which is what I need in order to know what I can safely change. |
| `status` | The HTTP status returned. | Finding what's broken. |
| `took` | How long the request took, in milliseconds. | Performance. |
| `host` | The `Host` header — which of the two hostnames you used. | The only way to tell old-host traffic from new-host traffic while both addresses serve the same container during the move to `libexdb.com`. |
| `request_id` | A random UUID generated for that one request. | Gives a log entry something to be referred to by. It is not derived from anything about you, is not returned to you, is not reused, and is not attached to any other line — it identifies a log entry, not a person and not a session. |

**`/health` is not logged at all.** It returns before any of the above happens.
Uptime monitors don't fill the logs.

**The API documentation pages are logged like anything else.** `/docs`,
`/redoc`, `/openapi.json`, and the files under `/static` that those pages
load, are ordinary requests and produce ordinary log lines. What they don't do
is reach anyone else — see
[Who else sees your requests](#who-else-sees-your-requests).

### What happens to a search you type

This is the part people most reasonably assume the worst about, so it gets its
own heading.

On a lookup like `/book/B01234567?region=us`, the query string is an
identifier for a book. Nothing personal.

On a search it is different. `/author/books?name=`, `/search?title=`,
`/search?author=` and `/quick-search?keywords=` all carry **free text that you
typed**. None of that text is written to a log.

The request line keeps the parameter's name and replaces its value with
`REDACTED`. If something you typed contained an `&`, the part after it arrives
looking like a parameter name of its own rather than a value — so anything
Libex doesn't recognise as one of its own parameter names is discarded along
with its text and replaced by a bare count, `_unrecognised=1`.

The search code writes a line of its own, and it follows the same rule. That
line records which fields were searched on, how many characters long a
quick-search query was, how a compound query split into segments, how long
Audible took and how many results came back. The text itself is in none of
them. A length is kept because a slow or empty search is worth correlating
with the size of the query that produced it — it doesn't tell me, or anyone
reading the logs later, what was typed.

Audible does receive what you typed, because there is no way to answer a
search without asking it. That is a different thing from Libex recording it,
and it's covered under
[Who else sees your requests](#who-else-sees-your-requests).

### The other log lines

Libex writes plenty of other log lines that have nothing to do with you:
cache hits and misses keyed by ASIN and region, database writes naming the
book, author or series just fetched, how long an Audible call took, how many
books a background job found, startup and shutdown messages. Those describe
Libex's own work on Audible's catalogue. The names in them came back from
Audible; they are catalogue data, not anything a caller typed. None of those
lines carry your IP, your user agent or any identifier tied to your request.

Two of them do record what a request asked for, and they're worth naming
rather than leaving to be discovered. Both belong to the bulk lookup
`/books?asins=`, which is the one place the request line deliberately withholds
what was asked for: `asins` isn't on the value allowlist, so there it reads
`asins=REDACTED`.

The first is the fallback itself. When Audible is unreachable and Libex falls
back to its own database, the warning recording that fallback lists the ASINs
that request asked for. The second is the database read behind it: if that read
fails as well, it writes a warning of its own naming the same ASINs — and it
does the same on the other path into that read, the one that fills in from the
database when only part of an Audible call failed. Keeping them is what makes
"which books did that outage affect" answerable at all, and the second line is
what keeps it answerable when the fallback is what broke.

An ASIN is a catalogue identifier — ten characters, letters and digits only,
checked against exactly that format before it ever reaches this code, so it
can't carry text you typed. Neither line carries anything about who asked.

Where an ASIN is part of the path instead — `/book/B01234567` and the like —
database warnings name it too, but that discloses nothing further: the request
line already records the path in its `url` field, as the table above says.

One further exception worth naming: if a request causes an unexpected error, the
error line and its stack trace can include whatever triggered it — and if
what triggered it was text you sent, that text can end up in the error line
too. That's not deliberate collection, but it's a real path by which your
input reaches the logs, so it belongs on this page rather than in a footnote.

### Your IP address is not recorded

Libex does not log your IP address. Not in full, not truncated, not hashed,
not in any derived form.

Earlier versions logged it, and a version that never shipped truncated it to a
`/24`. Both are gone. The address arrives in a header, because it has to for
the request to reach the server at all, and it is simply never read.

This is a deliberate step back from something that was working: a map of where
requests came from, built from those addresses. It was interesting and it is
not worth what it costs, so it was removed and the map with it.

**It isn't retroactive, and that matters more than the sentence above.** The
version running before the release that removed this did log the full address.
Records it already wrote still contain the address they captured, because
nothing goes back and rewrites a log. They expire on the schedules under
[How long it's kept](#how-long-its-kept) — up to 30 days in Axiom, and the
rotating file's own shorter window on the server — and until they do, they are
the only thing in Libex's logs that could be matched to a caller. From the
release onward, no new line has an address in it.

**Cloudflare still sees your real address**, because it terminates TLS for the
public instance. That is outside Libex's control and I'm not going to pretend
otherwise — see [Who else sees your requests](#who-else-sees-your-requests).

---

### What you type is not recorded either

Query parameters are logged so I can see which options consumers actually use.
What survives is decided by an allowlist, and it decides the names as well as
the values:

- **Kept with its value** — structural parameters: `region`, `limit`, `page`,
  `sort`, `order`, and the catalogue filters. These describe *how* a request
  was made. The value still has to look like the short token those parameters
  take — if it runs past 64 characters, or contains a `;` or an `=`, which is
  how a second query would be smuggled inside one value, it's redacted like
  anything else.
- **Kept as a bare name, value replaced with `REDACTED`** — the parameters
  Libex knows about but whose values it doesn't keep: `name`, `keywords`,
  `title`, `author`, `narrator` and the rest of what a person types, and
  `asins`, whose value is a list of catalogue identifiers that the request
  line doesn't hold on to either.
- **Dropped entirely** — everything else. A name Libex doesn't recognise
  isn't one of its parameter names, which means it is your text sitting where
  a name should be. That happens with no ill intent at all: an `&` inside
  something you typed splits it in two, and a query string with no `=` in it
  arrives as one long name and no value. Those are discarded outright and
  replaced by a count — `_unrecognised=2` — which tells me unexpected
  parameters turned up without keeping a character of them.

So a log line records that a search happened and which field it searched on.
What was searched for does not survive.

It is an allowlist rather than a blocklist on purpose. A blocklist leaks every
parameter added after it was written, silently, and nobody notices until
someone reads the logs. An allowlist withholds anything unclassified by
default, so the failure mode is a missing value rather than a leaked one — and
the same holds for the names, so a parameter added to a route but forgotten
here disappears from the logs instead of leaking into them.

**One honest exception, and its limit.** When something breaks in a way nobody
anticipated, the error is logged with its message so it can be diagnosed. If
that message happens to have been built from something you sent, that text is
in that one line. Removing it would mean not knowing why a release broke,
which is the thing this logging exists for.

The limit is the important half. This covers an error message that
*incidentally* carries your text — a library exception that quotes back the
URL it was handed, say. It is not cover for writing your text into a log line
on purpose and calling the result an error. Lines that did that, naming a
searched-for author, narrator or series, have been found and removed; that is
treated as a defect to fix, not an exception to claim. The genuine case is an
exceptional path rather than routine collection, and those lines age out with
everything else.

---

## What is not collected

Stated plainly, because the absences matter as much as the list above:

- **No cookies.** Libex never sets a cookie, never reads one, and never sends
  a `Set-Cookie` header. Nothing in a response stores state in your client.
- **No accounts, no logins, no API keys, no sessions.** There is no user
  record because there is no concept of a user.
- **No analytics or tracking beyond the request logging described here.** No
  Google Analytics, no Plausible, no Matomo, no Sentry, no tracking pixels,
  no fingerprinting. Axiom is the only third party Libex ships log records to.
- **No cross-request identifier.** Nothing persists between your requests and
  nothing links two of them together. There is no address, no cookie, no
  session, no fingerprint — two requests from you are indistinguishable from
  two requests by strangers.
- **No request bodies.** Every public endpoint is a `GET` and none of them
  accept a body.
- **No `Authorization` header, no `Referer`, no `Cookie` header.** Only the
  headers named in the table above are ever read for logging.
- **No caller data in the database at all.** Libex's Postgres database holds
  Audible metadata — books, authors, narrators, series, genres, chapters — and
  a cache of Audible responses keyed by ASIN and region. There is no table
  that holds anything about the people making requests. Purging caller data
  means purging logs; the database has nothing to purge.

---

## Who else sees your requests

Naming these is the point. "I don't sell your data" and "nobody else has it"
are different statements, and only the first is true.

**Cloudflare** fronts the public instance. Every request to `libexdb.com`
passes through Cloudflare's network before it reaches my server — Cloudflare
terminates TLS, which means it sees the full request, including your full,
untruncated IP address, before Libex sees anything. Cloudflare keeps its own
logs under its own policies and retention. I do not control them, I cannot
delete them, and nothing in Libex's code affects them. If Cloudflare's
handling of that data matters to you, Cloudflare's own privacy documentation
is the authority, not this page.

**Axiom** receives the log records described above — every field in that
table, and nothing that isn't in it. What it holds describes requests, not
requesters: no address, and nothing you typed.
Axiom is a hosted log service; it stores and indexes those records so I can
query them. I'm the only person I've given access to that dataset — but Axiom
is the company storing it, on its own infrastructure, under its own policies.
A vendor providing a service is still a third party holding your data, and
saying "only I can see the logs" would quietly skip over that.

**The operator of the server** can read the rotating log file written on the
machine, which contains the same fields. See the note under
[Who runs this](#who-runs-this).

**Audible** receives your search terms, because Libex is a proxy for Audible's
own API and there is no way to answer a search without asking Audible. What
Audible does *not* receive is anything about you: the outbound request carries
Libex's own fixed headers and comes from the server's IP address. Your IP,
your user agent and your host header are never forwarded.

**Nobody, for the documentation pages.** The interactive API documentation at
`/docs` and `/redoc` is rendered from files Libex serves itself, from
`/static`, at pinned and checksum-verified versions. Opening those pages
contacts no third party: no CDN, no font service, no external favicon, and no
logo fetched from the documentation tool's own vendor as it renders. The pages
do contain ordinary links out — an attribution link, specification URLs — and
those reach nobody unless you choose to click one.

**Nobody else.** I don't sell log data, share it, trade it, or hand it to
advertisers, data brokers or anyone else. The parties above have it because
they are how the service physically works, not because I gave it to them for
anything else.

---

## How long it's kept

**The rotating file on the server** is controlled by Libex's own code and by
the `LOG_RETENTION_DAYS` setting. It rotates at midnight and keeps that many
days of previous files — the default is 7. That much is verifiable in the
source.

**Container stdout** is retained according to the container runtime's log
configuration on the host, not by Libex.

**Axiom** expires records according to the retention configured on the dataset
in Axiom's own console, and **that is set to 30 days** — after 30 days Axiom
deletes the records.

I'm stating that as the person who set it, which is a different kind of claim
from everything above it and worth flagging as such. It is not a line of code
and it is not in this repository: nothing in the source enforces it, no test
checks it, and it could be changed in a browser tab without a single commit
appearing in the history. So it isn't something you can verify the way you can
verify the fields in the table above. What I can commit to is that if that
setting changes, this page changes with it.

**Cloudflare's retention** is Cloudflare's, and I have no visibility into it.

---

## Your rights, and what I can actually do

This is the section most likely to turn into a lie, so it's the one I've
written most carefully. If you're in the EU or the UK, data protection law
gives you rights over personal data about you. Here is what those rights run
into in a service with no accounts.

**The problem, stated once.** There is no identifier in Libex's logs that
belongs to you — not a weak one, not a shared one, none. No address is
recorded, nothing you typed is recorded on any routine path, and nothing links
one of your requests to another. There is no login you could use to prove
which lines were yours, and no line that is yours in the first place. The one
way your text can land in a log anyway — an error message that carried it
without meaning to — is covered under deletion below.

That isn't an evasion. It's the direct consequence of collecting nothing, and
it cuts both ways: it is also the reason I could not build a profile of you if
I wanted to.

**Access — what I can do:** nothing, and for a good reason. There is no field
in any log line that could be matched to you. Even if you told me your IP
address it would not help, because no line written since addresses were removed
has one in it. What a log line about your request looks like is described
exactly by the table above — that is the complete and honest answer to an
access request here. For records written before that release, see the note
under deletion.

**Deletion — what I can do:** there is nothing identifying to delete. No
record in the logs is attributable to you, so there is no set of rows that
constitutes "your data" to remove. Records age out on the retention schedule
regardless.

One limit on that, in time rather than in kind: records written *before* the
release that stopped logging addresses still carry the address they captured,
so for as long as they survive there is something in the logs that could be
matched to a caller. I'm not going to promise selective removal from a hosted
log store whose internals aren't mine — what I can tell you is that nothing new
is being written that way, and the old records expire on the schedule above.

If you believe something identifying about you has ended up in a log anyway —
an unhandled error that captured text you sent, most plausibly — tell me and I
will remove it. That is the one realistic case, and it is worth saying out
loud rather than hiding behind "we hold nothing about you."

**Objection, portability, rectification:** these all need data about you to
act on, and there isn't any. Cloudflare is a separate matter and holds your
real address under its own policy — that one is not mine to answer.

---

## Self-hosting

If you run your own Libex, **you** are the one collecting this data, not me.
Nothing from your instance reaches me or the public instance, and I have no
visibility into it whatsoever.

What carries over to your instance:

- The same request logging happens, with the same fields, to your stdout and
  your rotating log file at `./logs/libex.log`. The same rule applies
  there too — it's in the code, not in the public instance's configuration.
- **Nothing is sent to Axiom unless you set `AXIOM_TOKEN`.** Leave it empty
  (the default) and no log record leaves your server.
- There is no switch that turns the stdout or file logging off. If you don't
  want request lines recorded at all, raise `LOG_LEVEL` to `WARNING` or
  `ERROR` — the per-request line is logged at `INFO`, so a higher level drops
  it everywhere, including Axiom. `LOG_RETENTION_DAYS` controls how many days
  of rotated files are kept, and `0` means keep them forever rather than
  keep none.
- **There is no Cloudflare unless you put one there.** The public instance's
  edge is my deployment choice, not part of Libex.
- Your instance still calls Audible's API to answer requests, from your
  server's IP address. Search terms go to Audible; nothing about your users
  does.
- The `/docs` and `/redoc` pages are served from your own copy of the assets
  as well, so nobody who opens them on your instance contacts a third party
  either. That depends on `scripts/fetch_docs_assets.sh` having run, which the
  Docker build does for you; without it there is nothing to serve and both
  pages come up blank rather than quietly falling back to a CDN.

If you expose your instance to other people, this document isn't yours to
point them at — you're the one who decides what you log and who you ship it
to, and the answers will be different from mine.

---

## Changes

This file lives in the repository, so every change to it is in the git
history and anyone can see exactly what changed and when. If something
material changes about what's collected or who receives it, it changes here
in the same commit as the code that changed it — a privacy document that
lags the code is worse than none, because people rely on it.
