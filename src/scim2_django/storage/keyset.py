from collections.abc import Sequence
from typing import Any

from django.db.models import Q
from django.db.models.lookups import Exact
from django.db.models.lookups import GreaterThan
from django.db.models.lookups import IsNull
from django.db.models.lookups import LessThan
from scim2_server.keyset import Position


def _key_order(key: Any, ascending: bool) -> Any:
    """Per RFC 7644 §3.4.2.3, resources without a value come last when ascending and first when descending."""
    if ascending:
        return key.asc(nulls_last=True)

    return key.desc(nulls_first=True)


def _keyset_order(
    key: Any, ties: Sequence[Any], descending: bool, forward: bool
) -> list[Any]:
    """Return the ORDER BY terms of a search: the sort value, then the ties.

    A previous page is read backward, in the reverse order of the sort.
    """
    terms = [] if key is None else [_key_order(key, descending != forward)]
    return [*terms, *(tie.asc() if forward else tie.desc() for tie in ties)]


def _beyond(key: Any, tie_after: Q, anchor: Position, descending: bool) -> Q:
    """Return the condition keeping the rows past the anchor, in the reading direction.

    tie_after keeps the rows whose ties come after the ones of the anchor.
    The condition on the sort value is written out, as a row comparison
    would not place NULL where the sort does.
    """
    if key is None:
        return tie_after

    ascending = descending != anchor.forward
    key_is_null = Q(IsNull(key, True))
    if anchor.key is None and ascending:
        return key_is_null & tie_after

    if anchor.key is None:
        return Q(IsNull(key, False)) | (key_is_null & tie_after)

    if ascending:
        key_after = Q(GreaterThan(key, anchor.key)) | key_is_null
    else:
        key_after = Q(LessThan(key, anchor.key))

    return key_after | (Q(Exact(key, anchor.key)) & tie_after)
