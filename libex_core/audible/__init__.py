"""
Everything Libex does with Audible that needs no database, no cache and no
application settings.

The transport (client.py): region-aware URLs and headers, the shared httpx
client, and the concurrency and retry policy every outbound request goes
through. On top of it, one module per record kind -- books, chapters, series
-- each pairing a fetch function with the normalizer for what it fetches. A
fetch function takes the callable that makes the request (an AudibleGet) as
its first argument, so how a request leaves the process is always the
embedder's choice and set once, at process start.
"""
