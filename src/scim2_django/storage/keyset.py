import datetime
import decimal
import uuid
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.db.models import Q
from django.db.models.lookups import Exact
from django.db.models.lookups import GreaterThan
from django.db.models.lookups import IsNull
from django.db.models.lookups import LessThan
from scim2_models import InvalidCursorException

_TAGGED: dict[str, tuple[type, Callable[[Any], str], Callable[[str], Any]]] = {
    "datetime": (
        datetime.datetime,
        datetime.datetime.isoformat,
        datetime.datetime.fromisoformat,
    ),
    "date": (datetime.date, datetime.date.isoformat, datetime.date.fromisoformat),
    "decimal": (decimal.Decimal, str, decimal.Decimal),
    "uuid": (uuid.UUID, str, uuid.UUID),
}

_INTEGERS = range(-(2**63), 2**63)
"""The integers a database can store, in a BIGINT or in an SQLite INTEGER."""


def _encode(value: Any) -> Any:
    """Return a sort value as a value JSON can hold, tagged with its type when JSON has none."""
    if value is None or isinstance(value, bool | int | float | str):
        return value

    for tag, (python_type, dump, _) in _TAGGED.items():
        if isinstance(value, python_type):
            return {tag: dump(value)}

    raise TypeError(f"Cannot page on a sort value of type {type(value).__name__}")


def _decode(value: Any) -> Any:
    """Return the sort value a position holds."""
    if value is None or isinstance(value, bool | int | float | str):
        return value

    if not isinstance(value, dict) or len(value) != 1:
        raise InvalidCursorException

    ((tag, text),) = value.items()
    if tag not in _TAGGED or not isinstance(text, str):
        raise InvalidCursorException

    try:
        return _TAGGED[tag][2](text)
    except (ValueError, decimal.InvalidOperation):
        raise InvalidCursorException from None


@dataclass(frozen=True)
class _Anchor:
    """The row a cursor page starts after, or ends before.

    ties are the values that order the rows sharing a sort value: the
    identifier as text.
    """

    forward: bool
    key: Any
    ties: tuple[Any, ...]


def _anchor(position: Any) -> _Anchor | None:
    """Read the position a storage gave, or return None for the first page."""
    if position is None:
        return None

    if not isinstance(position, dict) or set(position) != {"d", "k", "i"}:
        raise InvalidCursorException

    if position["d"] not in ("next", "previous") or not isinstance(position["i"], str):
        raise InvalidCursorException

    ties: tuple[Any, ...] = (position["i"],)
    key = _decode(position["k"])
    if isinstance(key, int) and not isinstance(key, bool) and key not in _INTEGERS:
        raise InvalidCursorException

    return _Anchor(position["d"] == "next", key, ties)


def _position(direction: str, key: Any, ties: Sequence[Any]) -> Any:
    """Return the position of the page before or after a row, as a value JSON can hold.

    The sort value is null when the search has no sort.
    """
    return {"d": direction, "k": _encode(key), "i": ties[-1]}


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


def _beyond(key: Any, tie_after: Q, anchor: _Anchor, descending: bool) -> Q:
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
