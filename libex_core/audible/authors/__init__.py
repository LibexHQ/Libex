"""
Looking authors up on Audible: the profile, a search by name, and the books an
author is credited with.

profile.py fetches and normalizes an author's contributor record and asks
Audible's search suggestions which authors a name resolves to. The three
author-books walks each read a different Audible source, and none of them is
complete alone: screens.py reads the Android author-detail screen (the only
ASIN-exact source), catalog.py runs the windowed, category-sliced catalog
search attributed by author ASIN, and by_name.py runs the single-sort,
name-only catalog search for a caller that has a name and no ASIN. Each
returns what it found together with whether its walk reached a confirmed end;
deciding what to do with a walk that did not is the caller's.
"""
