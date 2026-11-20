import operator
from collections.abc import Callable
from collections.abc import Iterable
from functools import reduce
from typing import Any

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS
from django.db import models
from django.db.models import BooleanField
from django.db.models import Q
from django.db.models import Subquery
from django.db.models import Value
from django.db.models.lookups import Exact
from django.db.models.lookups import GreaterThan
from django.db.models.lookups import GreaterThanOrEqual
from django.db.models.lookups import IsNull
from django.db.models.lookups import LessThan
from django.db.models.lookups import LessThanOrEqual
from scim2_models import AttributeBinding
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
from .mapping import ResourceMapping
from .mapping import _Collection
from .mapping import _Column

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

_TRUE = Q(Value(True, output_field=BooleanField()))
_FALSE = Q(Value(False, output_field=BooleanField()))


def _as_key(column: _Column, value: Any) -> Any:
    """Return an identifier in the type of its field, or None when the field cannot hold it."""
    try:
        return column.field.to_python(value)  # type: ignore[union-attr]
    except ValidationError:
        return None


def _exact_key(column: _Column, value: str) -> Any:
    """Return an identifier in the type of its field, or None when the field holds no such identifier.

    The field holds it only when it gives back the same text, so "01" does not
    find the identifier 1.
    """
    key = _as_key(column, value)
    if key is None or str(key) != value:
        return None

    return key


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

    scope is the collection that a value selection, such as
    emails[type eq "work"], applies its filter to.
    """

    def __init__(
        self,
        mapping: ResourceMapping,
        scim_filter: ScimFilter[Any],
        comparator: _Comparator,
        scope: tuple[AttrPath, _Collection] | None = None,
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

        collections = [
            collection
            for collection, _ in self.mapping._lookup(binding)
            if collection is not None
        ]
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

    if column.textual and op in (CompareOperator.eq, CompareOperator.ne):
        return _compare_key(column, op, value)

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


def _compare_key(column: _Column, op: CompareOperator, value: str) -> Q:
    """Compare an identifier in its own type.

    The database then uses its index, and the result does not depend on how
    the database writes a UUID as text.
    """
    key = _exact_key(column, value)
    if key is None:
        return _TRUE if op == CompareOperator.ne else _FALSE

    field = models.F(column.key)
    if op == CompareOperator.ne:
        return _is_null(field) | ~_lookup(column, "exact", key)

    return _is_not_null(field) & _lookup(column, "exact", key)


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
        expression = comparator.column(column)

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
) -> tuple["models.QuerySet[Any]", "models.QuerySet[Any]"]:
    """Return the matching records, and the page of them a search asks for.

    The records are ordered by sort value, then by primary key: without the
    key, records sharing a sort value could change pages from a request to
    the next. Per RFC 7644 §3.4.2.3, records without a value come last when
    ascending and first when descending.
    """
    condition = _where(mapping, search_request, comparator)
    matching = mapping.record._default_manager.using(using).filter(condition)
    key = _sort_key(mapping, search_request, comparator)
    order: list[Any] = []
    if key is not None:
        descending = search_request.sort_order == SearchRequest.SortOrder.descending
        order.append(
            key.desc(nulls_first=True) if descending else key.asc(nulls_last=True)
        )

    collections = mapping._returned_collections(search_request)
    page = mapping._queryset(collections, using).filter(condition)
    page = page.order_by(*order, "pk")
    start = search_request.start_index_0 or 0
    if search_request.count is None:
        return matching, page[start:]

    return matching, page[start : start + search_request.count]
