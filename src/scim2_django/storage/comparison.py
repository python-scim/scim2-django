import itertools
import unicodedata
import warnings
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any

from django.db import connections
from django.db.models import Func
from django.db.models import Q
from django.db.models import TextField
from django.db.models import Value
from django.db.models.functions import Collate
from django.db.models.functions import Length
from django.db.models.functions import Lower
from django.db.models.functions import StrIndex
from django.db.models.functions import Substr
from django.db.models.lookups import Contains
from django.db.models.lookups import EndsWith
from django.db.models.lookups import Exact
from django.db.models.lookups import GreaterThan
from django.db.models.lookups import StartsWith
from scim2_models import AttributeBinding
from scim2_models import ScimPolicy
from scim2_models import default_comparison_key
from scim2_models.path import CompareOperator

from .mapping import ResourceMapping
from .mapping import _Column

_SUBSTRINGS = {
    CompareOperator.co: Contains,
    CompareOperator.sw: StartsWith,
    CompareOperator.ew: EndsWith,
}

_KEY_FUNCTIONS = itertools.count()


class _Comparator:
    """How a database compares the strings of a policy.

    The default lowers both sides with the lower() of the database, which
    approaches the default comparison key, and only lowers ASCII letters on
    some databases.
    """

    def __init__(self, policy: ScimPolicy) -> None:
        self.policy = policy

    def prepare(self, using: str) -> None:
        """Make a database connection ready to compare strings."""

    def exact(self, expression: Any) -> Any:
        """Return a string expression compared code point by code point, as identifiers are.

        The databases compare strings so by default, unless the collation
        of their columns ignores the case.
        """
        return expression

    def column(self, column: _Column) -> Any:
        """Return a string column in the form it is compared under."""
        if column.casefolded:
            return Lower(column.compared)

        return column.compared

    def operand(self, column: _Column, value: str) -> Any:
        """Return a string compared with a column, in the form it is compared under."""
        if column.casefolded:
            return Lower(Value(value, output_field=TextField()))

        return Value(value, output_field=TextField())

    def substring(self, op: CompareOperator, column: _Column, value: str) -> Q:
        """Return whether a string column contains, starts or ends with a value.

        Django escapes the wildcards of the value.
        """
        return Q(_SUBSTRINGS[op](self.column(column), self.operand(column, value)))


class _Collator(_Comparator):
    """Compare strings in the same form on both sides, ordered by code point as scim2-models orders them.

    form builds that form from a string expression. The value is normalized
    to NFC in Python, as the stored strings usually are.
    """

    def __init__(
        self,
        policy: ScimPolicy,
        form: Callable[[_Column, Any], Any],
        exact: Callable[[Any], Any],
    ) -> None:
        super().__init__(policy)
        self.form = form
        self._exact = exact

    def exact(self, expression: Any) -> Any:
        """Return a string expression compared code point by code point, whatever the collation of its column."""
        return self._exact(expression)

    def column(self, column: _Column) -> Any:
        """Return a string column in the form it is compared under."""
        return self.form(column, column.compared)

    def operand(self, column: _Column, value: str) -> Any:
        """Return a string normalized to NFC, in the form it is compared under."""
        normalized = unicodedata.normalize("NFC", value)
        return self.form(column, Value(normalized, output_field=TextField()))


def _postgresql_form(column: _Column, expression: Any) -> Any:
    """Return a string expression of PostgreSQL normalized to NFC, lowered if the column ignores the case, under the C collation.

    The lower() of PostgreSQL follows the locale of the database, so the
    comparison approaches the default comparison key without reaching it. The
    C collation orders the strings by code point.
    """
    normalized = _normalize(expression)
    if column.casefolded:
        normalized = _normalize(Lower(normalized))

    return Collate(normalized, "C")


def _mariadb_form(column: _Column, expression: Any) -> Any:
    """Return a string expression of MariaDB in UTF-8, lowered if the column ignores the case, under the binary collation.

    The comparison does not depend on the collation of the column. MariaDB
    cannot normalize a string to NFC, so a stored string in another form does
    not match its NFC form. lower() follows the case tables of Unicode under
    an uca1400 collation only. The collations without padding keep the
    trailing spaces.
    """
    if not column.casefolded:
        return _mariadb_exact(expression)

    lowered = Lower(Collate(_utf8mb4(expression), "utf8mb4_uca1400_nopad_as_cs"))
    return Collate(lowered, "utf8mb4_nopad_bin")


def _mariadb_exact(expression: Any) -> Any:
    """Return a string expression of MariaDB in UTF-8, under the binary collation without padding."""
    return Collate(_utf8mb4(expression), "utf8mb4_nopad_bin")


def _as_is(expression: Any) -> Any:
    return expression


def _normalize(expression: Any) -> Func:
    """Return the NFC form of a string expression, in PostgreSQL."""
    return Func(
        expression,
        template="normalize(%(expressions)s, NFC)",
        output_field=TextField(),
    )


class _KeyFunction(_Comparator):
    """Compare strings in SQLite with the comparison key of the policy itself.

    The key is a SQL function of the connection, which takes the URN of the
    attribute and the value, and returns NULL for a value the key refuses.
    LIKE is not used, as SQLite ignores the case of ASCII letters in it. Each
    comparator registers its own function, as storages may follow different
    policies.
    """

    def __init__(self, policy: ScimPolicy, mappings: Iterable[ResourceMapping]):
        super().__init__(policy)
        self.function = f"scim2_key_{next(_KEY_FUNCTIONS)}"
        self.mappings = list(mappings)
        self.bindings: dict[str, AttributeBinding] = {}

    def prepare(self, using: str) -> None:
        """Register the comparison key as a SQL function of a connection.

        Call it before each query: Django opens a connection for each thread,
        and opens a new one after closing the former.
        """
        connection = connections[using]
        connection.ensure_connection()
        connection.connection.create_function(
            self.function, 2, self.key, deterministic=True
        )

    def key(self, urn: str, value: Any) -> Any:
        """Return the comparison key of a stored value, or None if the key refuses it."""
        if value is None:
            return None

        try:
            return self._binding(urn).comparable(value, self.policy)
        except ValueError:
            return None

    def _binding(self, urn: str) -> AttributeBinding:
        """Return the attribute of a URN, looking again into the mappings for one bound since the last lookup."""
        if urn not in self.bindings:
            self.bindings = {
                column.binding.urn: column.binding
                for mapping in self.mappings
                for column in mapping._string_columns()
            }

        return self.bindings[urn]

    def column(self, column: _Column) -> Any:
        """Return the comparison key of a string column, computed by the SQL function."""
        return Func(
            Value(column.binding.urn),
            column.compared,
            function=self.function,
            output_field=TextField(),
        )

    def operand(self, column: _Column, value: str) -> Any:
        """Return the comparison key of a string, computed in Python."""
        key = column.binding.comparable(value, self.policy)
        return Value(key, output_field=TextField())

    def substring(self, op: CompareOperator, column: _Column, value: str) -> Q:
        """Return whether a string column contains, starts or ends with a value, compared by their keys."""
        left = self.column(column)
        right = self.operand(column, value)
        if op == CompareOperator.co:
            return Q(GreaterThan(StrIndex(left, right), 0))

        if op == CompareOperator.sw:
            return Q(Exact(Substr(left, 1, Length(right)), right))

        start = Length(left) - Length(right) + 1
        return Q(Exact(Substr(left, start), right))


_MARIADB_VERSION = (10, 10)
"""The first version of MariaDB with the uca1400 collations."""

_FORMS: dict[str, tuple[Callable[[_Column, Any], Any], Callable[[Any], Any]]] = {
    "postgresql": (_postgresql_form, _as_is),
    "mariadb": (_mariadb_form, _mariadb_exact),
}
"""How each database that approaches the default comparison key writes a string to compare it, and an identifier, by vendor."""


def _utf8mb4(expression: Any) -> Func:
    """Convert a string expression to utf8mb4 in MariaDB. The column may use another character set."""
    return Func(
        expression,
        template="CONVERT(%(expressions)s USING utf8mb4)",
        output_field=TextField(),
    )


def _vendor(connection: Any) -> str:
    """Return the name of the database of a connection, telling MariaDB apart from MySQL.

    MariaDB before 10.10 lacks the uca1400 collations, so it is named mysql,
    as a backend that does not tell whether it runs MariaDB.
    """
    if (
        connection.vendor == "mysql"
        and getattr(connection, "mysql_is_mariadb", False)
        and connection.mysql_version >= _MARIADB_VERSION
    ):
        return "mariadb"

    return str(connection.vendor)


def _comparator(
    vendor: str, policy: ScimPolicy, mappings: Iterable[ResourceMapping]
) -> _Comparator:
    """Return how a database compares strings, warning when it cannot follow the policy."""
    if vendor == "sqlite":
        return _KeyFunction(policy, mappings)

    if vendor in _FORMS:
        if policy.comparison_key is not default_comparison_key:
            warnings.warn(
                f"The {vendor} storage cannot compare strings with the comparison "
                "key of the policy: filters and uniqueness checks only approach it",
                stacklevel=2,
            )

        return _Collator(policy, *_FORMS[vendor])

    warnings.warn(
        f"The {vendor} storage compares strings with lower() and the collation "
        "of the database: filters and uniqueness checks only approach the "
        "comparison key of the policy, and may ignore the case of case exact "
        "attributes",
        stacklevel=2,
    )
    return _Comparator(policy)
