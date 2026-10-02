"""
Audible metadata fetched and normalized into data shaped after AudiMeta's.
The shapes are derived from AudiMeta's and differ from them in places; this is
not a drop-in replacement.

This package holds the pieces of that work that carry no database, no cache,
no web framework, and no environment configuration of their own, so it can be
embedded in another application without dragging any of that in behind it.

`__version__` is declared here because this package can move independently of
the hosted app that embeds it, and an embedder needs a version to pin against
that isn't tied to the hosted app's own release line.
"""

__version__ = "0.5.0"
