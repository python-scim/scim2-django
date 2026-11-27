import dataclasses
import operator
from collections.abc import Callable
from collections.abc import Iterable
from functools import reduce
from typing import Any
from typing import cast

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS
from django.db import models
from django.db.models import BooleanField
from django.db.models import Case
from django.db.models import ExpressionWrapper
from django.db.models import IntegerField
from django.db.models import Q
from django.db.models import Subquery
from django.db.models import Value
from django.db.models import When
from django.db.models.functions import Cast
from django.db.models.lookups import Exact
from django.db.models.lookups import GreaterThan
from django.db.models.lookups import GreaterThanOrEqual
from django.db.models.lookups import In
from django.db.models.lookups import IsNull
from django.db.models.lookups import LessThan
from django.db.models.lookups import LessThanOrEqual
from scim2_models import InvalidCursorException
from scim2_models import InvalidPathException
from scim2_models import SearchRequest
from scim2_models.path import CompareOperator
from scim2_server.keyset import Position
from scim2_server.mapping import Collection
from scim2_server.mapping import Column
from scim2_server.mapping.filters import entry_present
from scim2_server.mapping.filters import sort_target
from scim2_server.mapping.filters import where

from .comparison import _SUBSTRINGS
from .comparison import _Comparator
from .keyset import _beyond
from .keyset import _keyset_order
from .mapping import ResourceMapping
from .mapping import _as_key
from .mapping import _as_stored
from .mapping import _Collection
from .mapping import _Column
from .mapping import _exact_key

_ORDERINGS: dict[CompareOperator, Callable[[Any, Any], Any]] = {
    CompareOperator.eq: Exact,
    CompareOperator.gt: GreaterThan,
    CompareOperator.ge: GreaterThanOrEqual,
    CompareOperator.lt: LessThan,
    CompareOperator.le: LessThanOrEqual,
}

_LOOKUPS = {
    CompareOperator.eq: "exact",
    CompareOperator.gt: "gt",
    CompareOperator.ge: "gte",
    CompareOperator.lt: "lt",
    CompareOperator.le: "lte",
}
"""The names of the lookups, by operator."""

_NUMBERS = {
    "AutoField",
    "BigAutoField",
    "BigIntegerField",
    "DecimalField",
    "FloatField",
    "IntegerField",
    "PositiveBigIntegerField",
    "PositiveIntegerField",
    "PositiveSmallIntegerField",
    "SmallAutoField",
    "SmallIntegerField",
}
"""The internal types of the fields holding numbers."""

_INTEGERS = range(-(2**63), 2**63)
"""The integers a database can store, in a BIGINT or in an SQLite INTEGER."""

_TRUE = Q(Value(True, output_field=BooleanField()))
_FALSE = Q(Value(False, output_field=BooleanField()))


def _with_ids(
    column: _Column, values: Iterable[str | None], comparator: _Comparator
) -> Q:
    """Return the condition keeping the records whose identifier is one of values.

    The field is compared, so that the database uses its index. An identifier
    is found by its exact text only: "01" does not find the identifier "1".
    The text an identifier is read as is compared too, code point by code
    point, so that "u012" does not find the identifier "u12", and "ABC" does
    not find "abc" under a collation ignoring the case.
    """
    texts = [value for value in values if value is not None]
    keys = [
        key for key in (_exact_key(column, text) for text in texts) if key is not None
    ]
    condition = Q(**{f"{column.field_key}__in": keys})
    if column.transformed:
        condition &= Q(In(comparator.exact(column.compared), texts))
    elif not column.textual:
        condition &= Q(In(comparator.exact(models.F(column.field_key)), texts))

    return condition


def _is_null(expression: Any) -> Q:
    return Q(IsNull(expression, True))


def _is_not_null(expression: Any) -> Q:
    return Q(IsNull(expression, False))


class _Predicates:
    """The conditions of the mapping core, as Django Q objects.

    Every condition is true or false, never NULL, so that the negation of a
    comparison with a missing value holds. Django only ensures it for the
    comparisons of a field, not of an expression. The conditions on entries
    refer to the record of the outer query, which Django resolves when the
    subquery is built.
    """

    def __init__(self, comparator: _Comparator) -> None:
        self.comparator = comparator

    def true(self) -> Q:
        return _TRUE

    def false(self) -> Q:
        return _FALSE

    def any(self, conditions: Iterable[Q]) -> Q:
        return reduce(operator.or_, conditions, _FALSE)

    def all(self, conditions: Iterable[Q]) -> Q:
        return reduce(operator.and_, conditions, _TRUE)

    def negate(self, condition: Q) -> Q:
        return ~condition

    def is_null(self, column: Column) -> Q:
        return _is_null(_django(column).compared)

    def is_empty(self, column: Column) -> Q:
        return Q(Exact(_django(column).compared, ""))

    def compare(self, column: Column, op: CompareOperator, value: Any) -> Q:
        """Compare the field or the expression of a column with a value.

        A field is compared through its own lookups, as a filter by keyword
        does: an integer the field cannot hold then matches nothing, rather
        than failing in the database.
        """
        column = _django(column)
        value = _as_stored(value)
        name = _LOOKUPS[op]
        if column.expression is None:
            return Q(**{f"{column.key}__{name}": value})

        return Q(models.Field.get_lookups()[name](column.compared, value))  # type: ignore[call-arg]

    def compare_text(self, column: Column, op: CompareOperator, value: str) -> Q:
        """Compare a string column in the form the policy compares it under.

        A stored value the comparison key refuses is compared as no value, so
        that the comparison is false rather than NULL.
        """
        column = _django(column)
        left = self.comparator.column(column)
        if op in _SUBSTRINGS:
            condition = self.comparator.substring(op, column, value)
        else:
            right = self.comparator.operand(column, value)
            condition = Q(_ORDERINGS[op](left, right))

        return _is_not_null(left) & condition

    def compare_key(self, column: Column, value: Any) -> Q | None:
        """Compare an identifier in its own type.

        The database then uses its index, and the result does not depend on
        how the database writes a UUID as text.
        """
        column = _django(column)
        key = _exact_key(column, value)
        if key is None:
            return None

        condition = Q(**{f"{column.field_key}__exact": key})
        if column.transformed:
            condition &= Q(Exact(self.comparator.exact(column.compared), value))

        return condition

    def exists(
        self,
        collection: Collection,
        build: Callable[[Any], Q] | None = None,
    ) -> Q:
        condition = None if build is None else build(self)
        return collection.exists(condition)  # type: ignore[attr-defined,no-any-return]


def _django(column: Column) -> _Column:
    """Return a column built by the Django mapping."""
    return column  # type: ignore[return-value]


def _sort_key(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
) -> Any:
    """Return the expression the records of a mapping sort on.

    Return None when the search has no sortBy, or when the model of the
    mapping does not declare its attribute. A constant of an Entry sorts as
    its value when the entry exists, and as no value otherwise.
    """
    target = sort_target(mapping, search_request)
    if target is None:
        return None

    collection, column = target
    if column.is_constant:
        return _constant_key(mapping, column, comparator)

    expression: Any = _django(column).compared
    if column.is_string:
        # The text the database sorts on, without the conversion of a field.
        expression = ExpressionWrapper(
            comparator.column(_django(column)), output_field=models.TextField()
        )

    if collection is None:
        return expression

    return _first_entry(collection, expression)  # type: ignore[arg-type]


def _constant_key(
    mapping: ResourceMapping, column: Column, comparator: _Comparator
) -> Any:
    """Return the sort key of a constant of an Entry, which has no value when the entry does not exist.

    A string is in the form the database compares it under, and a string
    the policy cannot prepare has no value, as the stored strings do.
    """
    entry = mapping._entry_of(column)
    present = entry_present(_Predicates(comparator), entry)  # type: ignore[arg-type]
    if not column.is_string:
        value: Any = Value(column.constant, output_field=models.BooleanField())
    else:
        holder = _Column(column.binding, column.key, False)
        try:
            value = comparator.operand(holder, column.constant)
        except ValueError:
            value = Value(None, output_field=models.TextField())

    return Case(When(present, then=value), default=None)


def _first_entry(collection: _Collection, expression: Any) -> Subquery:
    """Return the value of the primary entry of a collection, or else of its first one."""
    entries = collection.entries()
    order: list[Any] = []
    primary = collection.columns.get("primary")
    if primary is not None:
        order.append(_django(primary).compared.desc(nulls_last=True))

    order.extend(collection.order())
    return Subquery(
        entries.order_by(*order)
        .annotate(scim_sort_key=expression)
        .values("scim_sort_key")[:1]
    )


def _where(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
) -> Q:
    """Return the condition keeping the records matching the filter of a search, or every record without filter."""
    return where(mapping, search_request, _Predicates(comparator), comparator.policy)  # type: ignore[no-any-return]


def _search(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
    using: str = DEFAULT_DB_ALIAS,
    anchor: Position | None = None,
) -> tuple["models.QuerySet[Any]", "models.QuerySet[Any]"]:
    """Return the matching records, and the page of them a search asks for.

    The records are ordered by sort value, then by identifier: without the
    identifier, records sharing a sort value could change pages from a
    request to the next. Per RFC 7644 §3.4.2.3, records without a value come
    last when ascending and first when descending. Each record of the page
    holds its sort value under the scim_sort_key attribute, when the search
    sorts.

    A cursor page is read from the anchor, in the reverse order for a
    previous page, and holds count + 1 records at most: the last one tells
    whether a page follows.
    """
    condition = _where(mapping, search_request, comparator)
    matching = mapping.record._default_manager.using(using).filter(condition)
    collections = mapping._returned_collections(search_request)
    page = mapping._queryset(collections, using).filter(condition)
    key = _sort_key(mapping, search_request, comparator)
    if key is not None:
        page = page.annotate(scim_sort_key=key)
        key = models.F("scim_sort_key")

    tie = _tie(mapping._id, comparator)
    descending = search_request.sort_order == SearchRequest.SortOrder.descending
    forward = anchor is None or anchor.forward
    page = page.order_by(*_keyset_order(key, [tie], descending, forward))
    if search_request.cursor is None:
        start = search_request.start_index_0 or 0
        if search_request.count is None:
            return matching, page[start:]

        return matching, page[start : start + search_request.count]

    if anchor is not None:
        if key is not None:
            anchor = _sort_anchor(page, anchor)

        value = _as_key(mapping._id, anchor.id)
        if value is None:
            raise InvalidCursorException

        after = GreaterThan if anchor.forward else LessThan
        tie_after = Q(after(tie, value))
        page = page.filter(_beyond(key, tie_after, anchor, descending))

    if search_request.count is None:
        return matching, page

    return matching, page[: search_request.count + 1]


def _tie(column: _Column, comparator: _Comparator) -> Any:
    """Return the expression ordering the records a sort leaves tied: their identifier field, compared code point by code point when it holds text."""
    field = models.F(column.field_key)
    if column.textual or column.transformed:
        return field

    return comparator.exact(field)


def _sort_anchor(page: "models.QuerySet[Any]", anchor: Position) -> Position:
    """Return an anchor whose sort value has the type of the sort key of a page.

    A sort value the key cannot hold, such as a text on an integer field
    since the mapping changed, raises InvalidCursorException.
    """
    output = page.query.annotations["scim_sort_key"].output_field
    return _typed_anchor(output, anchor)


def _typed_anchor(output: Any, anchor: Position) -> Position:
    """Return an anchor whose sort value has the type of a field, refusing a value the field cannot hold.

    An integer no database stores is refused too.
    """
    if anchor.key is None:
        return anchor

    try:
        key = output.to_python(anchor.key)
    except (ValidationError, TypeError, ValueError, OverflowError):
        raise InvalidCursorException from None

    if isinstance(key, int) and not isinstance(key, bool) and key not in _INTEGERS:
        raise InvalidCursorException

    return dataclasses.replace(anchor, key=key)


def _root_search(
    mappings: list[ResourceMapping],
    search_request: SearchRequest[Any],
    comparator: _Comparator,
    using: str = DEFAULT_DB_ALIAS,
    anchor: Position | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Return the number of matching records of several mappings, and the rows of the page a search asks for.

    Each row holds the position of the mapping as scim_position, the
    identifier of the record as text as scim_id, so that the identifiers of
    every model fit in one column, and its sort value as scim_sort_key. A
    mapping whose model does not declare the sort attribute sorts its records
    as having no value. The rows are ordered by sort value, then by position
    and identifier.
    """
    conditions = [_where(mapping, search_request, comparator) for mapping in mappings]
    keys = [_sort_key(mapping, search_request, comparator) for mapping in mappings]
    outputs = {
        position: _output_field(mapping, key)
        for position, (mapping, key) in enumerate(zip(mappings, keys, strict=True))
        if key is not None
    }
    if len({_family(output) for output in outputs.values()}) > 1:
        raise InvalidPathException(
            path=str(search_request.sort_by),
            detail=f"Cannot sort on '{search_request.sort_by}' at the root: "
            "the resource types store it in fields of different types",
        )

    output = next(iter(outputs.values()), None)
    if anchor is not None:
        anchor = _root_anchor(outputs, anchor)

    matching = []
    rows = []
    for position, (mapping, condition, key) in enumerate(
        zip(mappings, conditions, keys, strict=True)
    ):
        records = mapping.record._default_manager.using(using).filter(condition)
        # A union refuses the default ordering of a model in its branches.
        records = records.order_by()
        matching.append(
            records.annotate(scim_one=Value(1, output_field=IntegerField())).values(
                "scim_one"
            )
        )
        records = records.annotate(
            scim_position=Value(position, output_field=IntegerField()),
            scim_id=ExpressionWrapper(
                comparator.exact(mapping._id.compared),
                output_field=models.TextField(),
            ),
        )
        names = ["scim_position", "scim_id"]
        if output is not None:
            records = records.annotate(
                scim_sort_key=Cast(Value(None), output.clone()) if key is None else key
            )
            names.append("scim_sort_key")

        if anchor is not None:
            records = records.filter(
                _root_beyond(position, output, anchor, search_request)
            )

        rows.append(records.values(*names))

    total = matching[0].union(*matching[1:], all=True).count()
    sort_key = None if output is None else models.F("scim_sort_key")
    ties = [models.F("scim_position"), models.F("scim_id")]
    descending = search_request.sort_order == SearchRequest.SortOrder.descending
    forward = anchor is None or anchor.forward
    page = rows[0].union(*rows[1:], all=True)
    page = page.order_by(*_keyset_order(sort_key, ties, descending, forward))
    if search_request.cursor is None:
        start = search_request.start_index_0 or 0
        stop = None if search_request.count is None else start + search_request.count
        return total, list(page[start:stop])

    stop = None if search_request.count is None else search_request.count + 1
    return total, list(page[:stop])


def _family(field: Any) -> str:
    """Return the family of the values of a field: numbers form one family, as the databases compare them together."""
    internal_type = field.get_internal_type()
    return "number" if internal_type in _NUMBERS else internal_type


def _root_anchor(outputs: dict[int, Any], anchor: Position) -> Position:
    """Return an anchor of a search at the root, with a sort value of the type of the field of its row.

    The anchor has a sort value only when its resource type has a sort key.
    """
    if anchor.key is None:
        return anchor

    if anchor.type_index not in outputs:
        raise InvalidCursorException

    return _typed_anchor(outputs[anchor.type_index], anchor)


def _output_field(mapping: ResourceMapping, key: Any) -> Any:
    """Return the field of the values of a sort key of a mapping."""
    records = mapping.record._default_manager.annotate(scim_sort_key=key)
    return records.query.annotations["scim_sort_key"].output_field


def _root_beyond(
    position: int, output: Any, anchor: Position, search_request: SearchRequest[Any]
) -> Q:
    """Return the condition keeping the rows of the mapping at a position past the anchor, in the reading direction.

    Rows of an earlier resource type come before the anchor. Rows of a later
    resource type come after it.
    """
    anchor_position = cast(int, anchor.type_index)
    after = position > anchor_position if anchor.forward else position < anchor_position
    if position != anchor_position:
        tie_after = _TRUE if after else _FALSE
    elif anchor.forward:
        tie_after = Q(GreaterThan(models.F("scim_id"), anchor.id))
    else:
        tie_after = Q(LessThan(models.F("scim_id"), anchor.id))

    key = None if output is None else models.F("scim_sort_key")
    descending = search_request.sort_order == SearchRequest.SortOrder.descending
    return _beyond(key, tie_after, anchor, descending)
