import dataclasses
import operator
from collections.abc import Callable
from collections.abc import Iterable
from functools import reduce
from typing import Any

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS
from django.db import models
from django.db.models import BooleanField
from django.db.models import ExpressionWrapper
from django.db.models import IntegerField
from django.db.models import Q
from django.db.models import Subquery
from django.db.models import Value
from django.db.models.functions import Cast
from django.db.models.lookups import Exact
from django.db.models.lookups import GreaterThan
from django.db.models.lookups import GreaterThanOrEqual
from django.db.models.lookups import In
from django.db.models.lookups import IsNull
from django.db.models.lookups import LessThan
from django.db.models.lookups import LessThanOrEqual
from scim2_models import AttributeBinding
from scim2_models import InvalidCursorException
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import ScimFilter
from scim2_models import SearchRequest
from scim2_models.path import AttrPath
from scim2_models.path import CompareOperator
from scim2_models.path import Comparison
from scim2_models.path import FilterVisitor
from scim2_models.path import LogicalExpr
from scim2_models.path import LogicalOperator
from scim2_models.path import Not
from scim2_models.path import Present
from scim2_models.path import ValuePath
from scim2_models.path import coerce_value

from .comparison import _SUBSTRINGS
from .comparison import _Comparator
from .conversion import _as_stored
from .keyset import _Anchor
from .keyset import _beyond
from .keyset import _keyset_order
from .mapping import ResourceMapping
from .mapping import _Collection
from .mapping import _Column
from .mapping import _Entry

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

_TRUE = Q(Value(True, output_field=BooleanField()))
_FALSE = Q(Value(False, output_field=BooleanField()))


def _as_key(column: _Column, value: Any) -> Any:
    """Return an identifier in the type of its field, or None when the field cannot hold it.

    A transformed identifier is converted by to_field first.
    """
    try:
        if column.to_field is not None:
            value = column.to_field(value)

        return column.field.to_python(value)  # type: ignore[union-attr]
    except (TypeError, ValueError, ValidationError):
        return None


def _exact_key(column: _Column, value: str) -> Any:
    """Return an identifier in the type of its field, or None when the field holds no such identifier.

    The field holds it only when it gives back the same text, so "01" does not
    find the identifier 1. The expression of a transformed identifier tells
    it in the database instead.
    """
    key = _as_key(column, value)
    if column.transformed or key is None or str(key) == value:
        return key

    return None


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


def _unmapped(binding: AttributeBinding) -> InvalidFilterException:
    """Return the error of a filter on an attribute the mapping does not let filters read."""
    return InvalidFilterException(detail=f"Cannot filter on '{binding.urn}'")


def _is_null(expression: Any) -> Q:
    return Q(IsNull(expression, True))


def _is_not_null(expression: Any) -> Q:
    return Q(IsNull(expression, False))


class _FilterTranslator(FilterVisitor[Q]):
    """Turn a SCIM filter into a condition on the records of a mapping.

    The condition keeps the records whose resources ScimFilter.match keeps.
    Every condition is true or false, never NULL, so that the negation of a
    comparison with a missing value holds. Django only ensures it for the
    comparisons of a field, not of an expression.

    scope is the collection, or the Entry, that a value selection, such as
    emails[type eq "work"], applies its filter to.
    """

    def __init__(
        self,
        mapping: ResourceMapping,
        scim_filter: ScimFilter[Any],
        comparator: _Comparator,
        scope: tuple[AttrPath, "_Collection | _Entry"] | None = None,
    ) -> None:
        self.mapping = mapping
        self.filter = scim_filter
        self.comparator = comparator
        self.scope = scope

    def _resolve(
        self,
        attr_path: AttrPath,
        resolve: Callable[..., AttributeBinding | None],
    ) -> AttributeBinding | None:
        """Bind the attribute path of a comparison or of a presence test.

        Within a value selection of a simple attribute, such as
        tags[value eq "x"], value is the entry itself, and any other attribute
        matches nothing, as ScimFilter.match has it.
        """
        if self.scope is None:
            return resolve(attr_path, strict=False)

        scope_path, scope = self.scope
        if scope.simple:
            own = attr_path.attr.lower() == "value" and not attr_path.sub_attr
            return scope.binding if own else None

        return resolve(
            AttrPath(scope_path.attr, attr_path.attr, scope_path.uri), strict=False
        )

    def _lookup(
        self, binding: AttributeBinding
    ) -> list[tuple[_Collection | None, _Column | None]]:
        """Return the collections holding an attribute, with its column in each, as the mapping does.

        Within a value selection, only the collection of the selection holds
        the attribute.
        """
        pairs = self.mapping._lookup(binding)
        if self.scope is None:
            return pairs

        _, scope = self.scope
        if isinstance(scope, _Entry):
            return pairs

        return [
            (collection, column) for collection, column in pairs if collection is scope
        ]

    def _targets(
        self, binding: AttributeBinding
    ) -> list[tuple[_Collection | None, _Column]]:
        """Return the collections holding an attribute with its column in each, refusing an attribute that one of them does not let filters read."""
        pairs = self._lookup(binding)
        if any(column is None or not column.readable for _, column in pairs):
            raise _unmapped(binding)

        return pairs  # type: ignore[return-value]

    def _in_collection(self, collection: _Collection | None, condition: Q) -> Q:
        """Apply a condition to the entries of a collection, or to the record itself outside of any collection.

        Within a value selection, the condition already applies to the entries.
        """
        if collection is None or self.scope is not None:
            return condition

        return collection.exists(condition)

    def visit_comparison(self, node: Comparison) -> Q:
        """Keep the records with a value matching the comparison, in any collection holding the attribute."""
        binding = self._resolve(node.attr_path, self.filter.resolve_comparison)
        if binding is None:
            return _FALSE

        targets = self._targets(binding)
        value = coerce_value(binding, node.value, node.op)

        # 'emails ne "x"' holds when no email is "x", while
        # 'emails[value ne "x"]' holds when one email is not "x".
        if (
            node.op == CompareOperator.ne
            and targets[0][0] is not None
            and self.scope is None
        ):
            return ~_any(
                collection.exists(  # type: ignore[union-attr]
                    _compare(self.comparator, column, CompareOperator.eq, value)
                )
                for collection, column in targets
            )

        return _any(
            self._in_collection(
                collection, _compare(self.comparator, column, node.op, value)
            )
            for collection, column in targets
        )

    def visit_present(self, node: Present) -> Q:
        """Keep the records with a value for the attribute.

        A complex attribute is present when one of its sub-attributes is.
        """
        binding = self._resolve(node.attr_path, self.filter.resolve)
        if binding is None:
            return _FALSE

        pairs = self._lookup(binding)
        in_collection = pairs[0][0] is not None
        if in_collection and binding.sub_field_name is None and pairs[0][1] is None:
            return _any(collection.exists() for collection, _ in pairs)  # type: ignore[union-attr]

        if all(column is not None and column.readable for _, column in pairs):
            return _any(
                self._in_collection(collection, _present(column))  # type: ignore[arg-type]
                for collection, column in pairs
            )

        sub_columns = [
            column for column in self.mapping._sub_columns(binding) if column.readable
        ]
        if in_collection or not sub_columns:
            raise _unmapped(binding)

        return _any(_present(column) for column in sub_columns)

    def visit_not(self, node: Not) -> Q:
        """Keep the records the inner filter does not keep."""
        return ~self.visit(node.expr)

    def visit_logical_expr(self, node: LogicalExpr) -> Q:
        """Combine the conditions of the terms with AND or OR."""
        conditions = [self.visit(term) for term in node.terms]
        if node.op == LogicalOperator.and_:
            return _all(conditions)

        return _any(conditions)

    def visit_value_path(self, node: ValuePath) -> Q:
        """Keep the records with an entry of the attribute matching the inner filter."""
        binding = self.filter.resolve(node.attr_path, strict=False)
        if binding is None:
            return _FALSE

        collections: list[_Collection | _Entry] = [
            collection
            for collection, _ in self.mapping._lookup(binding)
            if collection is not None
        ]
        entry = self.mapping._entries.get((binding.model, binding.field_name))
        if entry is not None:
            collections = [entry]

        if not collections:
            raise _unmapped(binding)

        return _any(
            collection.exists(
                _FilterTranslator(
                    self.mapping,
                    self.filter,
                    self.comparator,
                    (node.attr_path, collection),
                ).visit(node.val_filter)
            )
            for collection in collections
        )


def _any(conditions: Iterable[Q]) -> Q:
    """Return the condition that one of the conditions holds."""
    return reduce(operator.or_, conditions)


def _all(conditions: Iterable[Q]) -> Q:
    """Return the condition that every condition holds."""
    return reduce(operator.and_, conditions)


def _present(column: _Column) -> Q:
    """Per RFC 7644 §3.4.2.2, an empty string is no value."""
    expression = column.compared
    if column.is_string:
        return _is_not_null(expression) & ~Q(Exact(expression, ""))

    return _is_not_null(expression)


def _compare(
    comparator: _Comparator, column: _Column, op: CompareOperator, value: Any
) -> Q:
    """Compare a column with a value, strings in the form the policy compares them under.

    A string the policy cannot prepare is equal to no other value, so only ne
    holds against it.
    """
    expression = column.compared
    value = _as_stored(value)
    if value is None:
        if op == CompareOperator.eq:
            return _is_null(expression)

        if op == CompareOperator.ne:
            return _is_not_null(expression)

        return _FALSE

    if column.keyed and op in (CompareOperator.eq, CompareOperator.ne):
        return _compare_key(column, op, value, comparator)

    if not column.is_string:
        if op == CompareOperator.ne:
            return _is_null(expression) | ~_lookup(column, "exact", value)

        if op in _SUBSTRINGS:
            return _FALSE

        return _is_not_null(expression) & _lookup(column, _LOOKUPS[op], value)

    try:
        column.binding.comparable(value, comparator.policy)
    except ValueError:
        return _TRUE if op == CompareOperator.ne else _FALSE

    left = comparator.column(column)
    if op == CompareOperator.ne:
        return _is_null(left) | ~Q(Exact(left, comparator.operand(column, value)))

    if op in _SUBSTRINGS:
        condition = comparator.substring(op, column, value)
    else:
        condition = Q(_ORDERINGS[op](left, comparator.operand(column, value)))

    return _is_not_null(left) & condition


def _compare_key(
    column: _Column, op: CompareOperator, value: str, comparator: _Comparator
) -> Q:
    """Compare an identifier in its own type.

    The database then uses its index, and the result does not depend on how
    the database writes a UUID as text.
    """
    key = _exact_key(column, value)
    if key is None:
        return _TRUE if op == CompareOperator.ne else _FALSE

    field = models.F(column.field_key)
    condition = Q(**{f"{column.field_key}__exact": key})
    if column.transformed:
        condition &= Q(Exact(comparator.exact(column.compared), value))

    if op == CompareOperator.ne:
        return _is_null(field) | ~condition

    return _is_not_null(field) & condition


def _lookup(column: _Column, name: str, value: Any) -> Q:
    """Compare the field or the expression of a column with a value.

    A field is compared through its own lookups, as a filter by keyword
    does: an integer the field cannot hold then matches nothing, rather than
    failing in the database.
    """
    if column.expression is None:
        return Q(**{f"{column.key}__{name}": value})

    return Q(models.Field.get_lookups()[name](column.compared, value))  # type: ignore[call-arg]


def _sort_key(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
) -> Any:
    """Return the expression the records of a mapping sort on.

    Return None when the search has no sortBy, or when the model of the
    mapping does not declare its attribute.
    """
    binding = search_request.sort_binding(mapping.model)
    if binding is None:
        return None

    collection, column = mapping._lookup(binding)[0]
    if (
        column is None
        or not column.readable
        or (collection is not None and not collection.owned)
    ):
        raise InvalidPathException(
            path=str(search_request.sort_by),
            detail=f"Cannot sort on '{binding.urn}'",
        )

    expression: Any = column.compared
    if column.is_string:
        # The text the database sorts on, without the conversion of a field.
        expression = ExpressionWrapper(
            comparator.column(column), output_field=models.TextField()
        )

    if collection is None:
        return expression

    return _first_entry(collection, expression)


def _first_entry(collection: _Collection, expression: Any) -> Subquery:
    """Return the value of the primary entry of a collection, or else of its first one."""
    entries = collection.entries()
    order: list[Any] = []
    primary = collection.columns.get("primary")
    if primary is not None:
        order.append(primary.compared.desc(nulls_last=True))

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
    if search_request.filter is None:
        return _TRUE

    scim_filter = ScimFilter[mapping.model](str(search_request.filter))  # type: ignore[name-defined]
    return _FilterTranslator(mapping, scim_filter, comparator).visit(scim_filter.ast)


def _search(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
    using: str = DEFAULT_DB_ALIAS,
    anchor: _Anchor | None = None,
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

        value = _as_key(mapping._id, anchor.ties[0])
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


def _sort_anchor(page: "models.QuerySet[Any]", anchor: _Anchor) -> _Anchor:
    """Return an anchor whose sort value has the type of the sort key of a page.

    A sort value the key cannot hold, such as a text on an integer field
    since the mapping changed, raises InvalidCursorException.
    """
    output = page.query.annotations["scim_sort_key"].output_field
    return _typed_anchor(output, anchor)


def _typed_anchor(output: Any, anchor: _Anchor) -> _Anchor:
    """Return an anchor whose sort value has the type of a field, refusing a value the field cannot hold."""
    if anchor.key is None:
        return anchor

    try:
        key = output.to_python(anchor.key)
    except (ValidationError, TypeError, ValueError):
        raise InvalidCursorException from None

    return dataclasses.replace(anchor, key=key)


def _root_search(
    mappings: list[ResourceMapping],
    search_request: SearchRequest[Any],
    comparator: _Comparator,
    using: str = DEFAULT_DB_ALIAS,
    anchor: _Anchor | None = None,
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
        anchor = _root_anchor(outputs, len(mappings), anchor)

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


def _root_anchor(outputs: dict[int, Any], count: int, anchor: _Anchor) -> _Anchor:
    """Return an anchor of a search at the root, with a sort value of the type of the field of its row.

    The anchor needs the position of a searched resource type, and a sort
    value only when that type has a sort key.
    """
    position = anchor.ties[0]
    if position not in range(count):
        raise InvalidCursorException

    if anchor.key is None:
        return anchor

    if position not in outputs:
        raise InvalidCursorException

    return _typed_anchor(outputs[position], anchor)


def _output_field(mapping: ResourceMapping, key: Any) -> Any:
    """Return the field of the values of a sort key of a mapping."""
    records = mapping.record._default_manager.annotate(scim_sort_key=key)
    return records.query.annotations["scim_sort_key"].output_field


def _root_beyond(
    position: int, output: Any, anchor: _Anchor, search_request: SearchRequest[Any]
) -> Q:
    """Return the condition keeping the rows of the mapping at a position past the anchor, in the reading direction.

    Rows of an earlier resource type come before the anchor. Rows of a later
    resource type come after it.
    """
    anchor_position, anchor_id = anchor.ties
    after = position > anchor_position if anchor.forward else position < anchor_position
    if position != anchor_position:
        tie_after = _TRUE if after else _FALSE
    elif anchor.forward:
        tie_after = Q(GreaterThan(models.F("scim_id"), anchor_id))
    else:
        tie_after = Q(LessThan(models.F("scim_id"), anchor_id))

    key = None if output is None else models.F("scim_sort_key")
    descending = search_request.sort_order == SearchRequest.SortOrder.descending
    return _beyond(key, tie_after, anchor, descending)
