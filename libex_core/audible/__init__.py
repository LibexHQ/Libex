"""
Everything Libex does with Audible that needs no database, no cache and no
application settings.

The transport (client.py): region-aware URLs and headers, the shared httpx
client, and the concurrency and retry policy every outbound request goes
through. On top of it, one module per kind of record or lookup, each pairing
its fetch functions with the normalizer for what they fetch. A
fetch function takes the callable that makes the request (an AudibleGet) as
its first argument, so how a request leaves the process is always the
embedder's choice and set once, at process start.
"""
