"""
The Audible transport: region-aware URLs and headers, the shared httpx
client, and the retry/back-off policy every outbound Audible request goes
through. Carries no database, no cache, and no application settings of its
own -- how it egresses is set once, at process start, by whoever embeds it.
"""
