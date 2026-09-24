# invincible/core/scope.py
"""Session-scope sentinel shared by the run and continuity stores.

``session_pk`` - the surrogate id of the ``sessions`` row whose ownership
a caller has PROVEN - is the multi-tenant isolation predicate. The stores
additionally carry a legacy string-scoped path, for single-tenant callers
(unit tests, local tooling) that never had a principal to resolve.

Those two must never be conflated. A request path that resolves ownership
and FAILS holds ``session_pk = None``; if a store reads that as "no scope
requested" it falls back to matching the caller-supplied client string
with no owner predicate, which is a cross-tenant read - exactly the bug in
``docs/DEEP-CODE-REVIEW-2026-09-24.md`` finding 1.

So the default is :data:`UNSCOPED`: the explicit "this call is
single-tenant by construction" opt-in, spelled by OMITTING the argument.
Passing ``None`` deliberately means "scoped, but the owner did not
resolve", and every store treats it as an empty result (reads) or an
:class:`UnresolvedScopeError` (writes)::

    await runs.recent(session_id="s")                # legacy, unscoped
    await runs.recent(session_id=s, session_pk=pk)   # scoped
    await runs.recent(session_id=s, session_pk=None) # -> [] (unresolved)

The distinction is the whole point, so the sentinel is a unique object
rather than a second magic value.
"""


class UnresolvedScopeError(ValueError):
    """A write was attempted with ``session_pk=None``.

    The caller asked for ownership scoping but resolved no owning session,
    so the write would land unscoped. Failing loudly matches the invariant
    ``core/session_store.py`` states for every other store: a call site
    that forgets its principal must fail rather than silently mix users'
    data.
    """


class _Unscoped:
    """Sentinel type: the caller passed no scope argument at all."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSCOPED"


UNSCOPED = _Unscoped()
