"""
Counting attempts, for pages anyone can reach without signing in.

A counter lives in the cache for a window of time; once it reaches its limit the page turns
the visitor away until the window runs out. Keys are hashed, so an address or an ID typed by
a stranger never becomes part of a cache key as written.
"""

import hashlib

from django.core.cache import cache


def key(scope, *parts):
    return f"{scope}:" + hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def count(cache_key, window):
    """Add one to a counter, starting it if it is new; the window runs from the first attempt."""
    cache.add(cache_key, 0, window)
    try:
        return cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, window)
        return 1


def reached(cache_key, limit):
    return cache.get(cache_key, 0) >= limit
