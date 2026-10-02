"""
Audible metadata fetched and normalized into data shaped after AudiMeta's.
The shapes are derived from AudiMeta's and differ from them in places; this is
not a drop-in replacement.

This package holds the pieces of that work that carry no database, no cache,
no web framework, and no environment configuration of their own, so it can be
embedded in another application without dragging any of that in behind it.

The library reads no environment variable: every setting arrives as an
argument. The one exception is the command line, whose environment module
(`libex_core.cli.environment`) is the only code in the package that touches
the process environment, and only when it is asked for a configuration.

Importing `libex_core` imports nothing else: the top level re-exports no
names, so `import libex_core` costs no more than an empty module and pulls in
neither httpx, pydantic nor the storage libraries. What an embedder uses is
imported from where it lives:

- `libex_core.audible.client`: `LibexClient`, the one object that reaches
  Audible, and `AudibleGet`, the shape of its `get`.
- `libex_core.lookup`: one function per endpoint, returning the published
  models.
- `libex_core.models`: the published response models.
- `libex_core.exceptions`: `LibexException`, its subclasses and `ErrorCode`.
- `libex_core.asin`: ASIN validation and normalisation.
- `libex_core.storage`: the optional local store, which needs the `storage`
  extra and loads its libraries only on first use.

Everything else under this package, `libex_core.cli` included, is internal
and may change in any release; the `libex-core` command's own options and
exit statuses are the public contract of the command line.

`__version__` is declared here because this package can move independently of
the hosted app that embeds it, and an embedder needs a version to pin against
that isn't tied to the hosted app's own release line.
"""

__version__ = "0.21.0"

__all__ = ["__version__"]
