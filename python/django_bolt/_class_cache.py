"""Keep a value for each class, for the response scan and the dict-body plans.

A dict finds a key by equality. A metaclass with its own ``__eq__`` and
``__hash__`` can make one class equal to another class. A plain dict then
gives the value of one class to the other class. Thus each entry holds its
class, and ``recall`` gives the value only to that class itself.

Each cache keeps at most ``CACHE_LIMIT`` classes. When a cache is full, its
oldest class goes: a dict keeps the order of insertion. Thus a cache keeps a
bounded number of classes alive when an app makes classes at runtime.
"""

from __future__ import annotations

from typing import Any

CACHE_LIMIT = 512

# The value of ``recall`` when the cache has no entry for the class.
MISSING: Any = object()


def remember(cache: dict[Any, tuple[Any, Any]], cls: Any, value: Any) -> None:
    """Keep ``value`` for ``cls`` in ``cache``, in an entry that holds ``cls``."""
    if len(cache) >= CACHE_LIMIT:
        try:
            del cache[next(iter(cache))]
        except (KeyError, RuntimeError, StopIteration):
            # Another thread changed the cache. It is a cache, so the entry stays out.
            return
    try:
        cache[cls] = (cls, value)
    except TypeError:
        # A class whose metaclass makes it unhashable gets its value each time.
        return


def recall(cache: dict[Any, tuple[Any, Any]], cls: Any) -> Any:
    """Return the value that ``cache`` keeps for ``cls`` itself, or ``MISSING``."""
    try:
        entry = cache.get(cls)
    except TypeError:
        return MISSING
    if entry is None or entry[0] is not cls:
        return MISSING
    return entry[1]
