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
user agent, the host header you used, and the query parameter *names* with
their values allowlisted — structural options like `region` and `limit` keep
their values, and anything you typed is replaced with `REDACTED`.

**No IP address. Nothing you searched for. No cookie, no identifier, nothing
that links one of your requests to another.**

Those lines go to the container's stdout, to a rotating file on the server,
and to **Axiom**, a third-party log service. **Cloudflare** sits in front of
the public instance, terminates TLS, and keeps its own logs — including your
real address — which I don't control and can't reach.

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
| `url` | The path only, e.g. `/author/books`. | Which endpoints are used and which are failing. |
| `query` | Query parameter names, with **values allowlisted** — structural params keep their values, anything you typed is replaced with `REDACTED`. See [below](#what-you-type-is-not-recorded-either). | The path alone can't tell me which parameters consumers actually use, which is what I need in order to know what I can safely change. |
| `status` | The HTTP status returned. | Finding what's broken. |
| `took` | How long the request took, in milliseconds. | Performance. |
| `host` | The `Host` header — which of the two hostnames you used. | The only way to tell old-host traffic from new-host traffic while both addresses serve the same container during the move to `libexdb.com`. |
| `request_id` | A random UUID generated for that one request. | Gives a log entry something to be referred to by. It is not derived from anything about you, is not returned to you, is not reused, and is not attached to any other line — it identifies a log entry, not a person and not a session. |

**`/health` is not logged at all.** It returns before any of the above happens.
Uptime monitors don't fill the logs.

**The API documentation pages are logged like anything else.** `/docs`,
`/redoc` and `/openapi.json` are ordinary requests and produce ordinary log
lines. They also load assets from other people's servers — see
[Who else sees your requests](#who-else-sees-your-requests).

### What happens to a search you type

This is the part most people won't expect, so it gets its own heading.

On a lookup like `/book/B01234567?region=us`, the query string is an
identifier for a book. Nothing personal.

On a search, it isn't. `/author/books?name=`, `/search?title=`, `/search?author=`,
`/quick-search?keywords=` and the rest all carry **free text that you typed**,
and it is written into the log verbatim. Search endpoints also produce a
*second* log line from inside the search service itself, recording the search
terms as their own fields (`search_params`, `keywords`, `author_name`, and on
one fallback path the author and title parsed out of a compound query).

A search box is a search box: whatever you put in it is what gets recorded.
I don't attempt to filter, redact or classify that text, and I'd rather tell
you that than let you assume otherwise.

### The other log lines

Libex writes plenty of other log lines that have nothing to do with you:
cache hits and misses keyed by ASIN and region, how long an Audible call
took, how many books a background job found, startup and shutdown messages.
Those describe Libex's own work on Audible's catalogue, and none of them
carry your IP, your user agent or any identifier tied to your request.

The one exception worth naming: if a request causes an unexpected error, the
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

**Cloudflare still sees your real address**, because it terminates TLS for the
public instance. That is outside Libex's control and I'm not going to pretend
otherwise — see [Who else sees your requests](#who-else-sees-your-requests).

---

### What you type is not recorded either

Query parameters are logged so I can see which options consumers actually use.
The values are allowlisted, not filtered:

- **Kept** — structural parameters. `region`, `limit`, `page`, `sort`,
  `order`, and the catalogue filters. These describe *how* a request was made.
- **Replaced with `REDACTED`** — everything else, including `name`,
  `keywords`, `title`, `author`. These are what a person typed.

The parameter's name survives, so a log line records that a search happened
and which field it searched on. What was searched for does not survive.

It is an allowlist rather than a blocklist on purpose. A blocklist leaks every
parameter added after it was written, silently, and nobody notices until
someone reads the logs. An allowlist redacts anything unclassified by default,
so the failure mode is a missing value rather than a leaked one.

**One honest exception.** When something breaks in a way nobody anticipated,
the error is logged with its message so it can be diagnosed. If that message
was built from something you sent, that text is in that one line. Removing it
would mean not knowing why a release broke, which is the thing this logging
exists for. It is an exceptional path, not routine collection, and those lines
age out with everything else.

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
table, including the query string and therefore including your search text.
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

**cdn.jsdelivr.net, fonts.googleapis.com and fastapi.tiangolo.com** — but only
if you open the API documentation in a browser. `/docs` loads Swagger UI from
jsDelivr; `/redoc` loads ReDoc from jsDelivr and a font from Google Fonts;
both load a favicon from `fastapi.tiangolo.com`. Loading those assets sends
your browser's full IP address and user agent to those third parties, exactly
as any third-party asset on any web page does. This is the framework's default
behaviour and I haven't changed it. **Machine clients calling the API are not
affected** — this only happens if you visit the docs pages in a browser.

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
in Axiom's own console. That is not a line of code and it is not in this
repository, so it is not something you can check by reading the source, and it
is not something I'm going to state here as though it were.

The maintainer has confirmed that dataset is set to **30 days**, after which
Axiom deletes the records. The README states the same figure.

**Cloudflare's retention** is Cloudflare's, and I have no visibility into it.

---

## Your rights, and what I can actually do

This is the section most likely to turn into a lie, so it's the one I've
written most carefully. If you're in the EU or the UK, data protection law
gives you rights over personal data about you. Here is what those rights run
into in a service with no accounts.

**The problem, stated once.** There is no identifier in Libex's logs that
belongs to you — not a weak one, not a shared one, none. No address is
recorded, nothing you typed is recorded, and nothing links one of your
requests to another. There is no login you could use to prove which lines were
yours, and no line that is yours in the first place.

That isn't an evasion. It's the direct consequence of collecting nothing, and
it cuts both ways: it is also the reason I could not build a profile of you if
I wanted to.

**Access — what I can do:** nothing, and for a good reason. There is no field
in any log line that could be matched to you. Even if you told me your IP
address it would not help, because that address was never written down. What a
log line about your request looks like is described exactly by the table
above — that is the complete and honest answer to an access request here.

**Deletion — what I can do:** there is nothing identifying to delete. No
record in the logs is attributable to you, so there is no set of rows that
constitutes "your data" to remove. Records age out on the retention schedule
regardless.

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
- The `/docs` and `/redoc` pages still load assets from jsDelivr and Google
  Fonts for anyone who opens them in a browser.

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
