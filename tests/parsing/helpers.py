"""Small helpers for parsing inline C snippets and inspecting symbols."""

from kernel_atlas import cparse

KINDS = frozenset(cparse.DEFAULT_KINDS)


def parse(src: str, kinds=KINDS, calls=False):
    return cparse.parse_source(src.encode(), kinds, calls)


def by_name(symbols):
    return {s.name: s for s in symbols}
