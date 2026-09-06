"""Convenience helpers for collecting and comparing query results."""

from kernel_atlas.queries import query


def names(entries):
    return sorted(e.name for e in entries)


def sib(conn, target, level="auto", kinds=None, include_self=False, **kw):
    t = query.resolve(conn, target).target
    assert t is not None, target
    scope = query.build_scope(conn, t, level)
    ks = kinds or query.default_kinds(t)
    entries = query.collect(conn, scope, ks, **kw)
    if not include_self:
        entries = [e for e in entries
                   if not (e.path == t.path and e.name == t.name)]
    return entries
