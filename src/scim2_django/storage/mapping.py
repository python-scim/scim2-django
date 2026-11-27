import base64
import datetime
import weakref
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core import checks
from django.core.exceptions import FieldDoesNotExist
from django.core.exceptions import FieldError
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS
from django.db import models
from django.db.models import CASCADE
from django.db.models import Exists
from django.db.models import F
from django.db.models import ForeignObjectRel
from django.db.models import Func
from django.db.models import OuterRef
from django.db.models import Prefetch
from django.db.models import Q
from django.db.models import Value
from django.db.models.functions import Cast
from django.db.models.functions import Concat
from django.db.models.functions import NullIf
from django.db.models.functions import Substr
from django.urls import get_resolver
from scim2_models import AttributeBinding
from scim2_models import Mutability
from scim2_models import Resource
from scim2_models import Schema
from scim2_server import mapping as core
from scim2_server.mapping import MappingProblem
from scim2_server.mapping.declaration import key as _key
from scim2_server.mapping.declaration import mutabilities as _mutabilities
from scim2_server.mapping.declaration import never_returned as _never_returned
from scim2_server.mapping.mapping import Problems

_MAPPINGS: "weakref.WeakSet[ResourceMapping]" = weakref.WeakSet()
"""The mappings created so far, which the system check validates."""

_TEXT_FIELDS = (models.CharField, models.TextField)

_UUID_PARTS = ((1, 8), (9, 4), (13, 4), (17, 4), (21, 12))

_CHECK_IDS = {
    "hidden-in-entries": "scim2_django.E007",
    "read-only-in-entries": "scim2_django.E007",
    "entry-fields": "scim2_django.E007",
    "single-link-null": "scim2_django.E003",
    "unwritten": "scim2_django.W002",
}
"""The identifiers of the system checks, by code of the problems of the mapping core."""


class _UUIDText(Func):
    """The text of a UUID, with its dashes, as clients read it.

    Databases without a UUID type store it as 32 hexadecimal digits, which
    a cast to text returns without dashes.
    """

    output_field = models.TextField()

    def as_sql(  # type: ignore[override]
        self, compiler: Any, connection: Any, **extra_context: Any
    ) -> Any:
        (expression,) = self.get_source_expressions()
        if connection.features.has_native_uuid_field:
            return compiler.compile(Cast(expression, models.TextField()))

        parts: list[Any] = []
        for start, length in _UUID_PARTS:
            if parts:
                parts.append(Value("-"))

            parts.append(Substr(expression, start, length))

        text = Concat(*parts, output_field=models.TextField())
        return compiler.compile(text.resolve_expression(compiler.query))


class Attribute(core.Attribute):
    """A field holding a SCIM attribute that the storage only reads.

    The storage reads every attribute, a write-only one included, such as a
    password: the server never returns it, but sends it back when a
    replacement leaves it out.

    :param field: The name of the field holding the value.
    :param writable: Whether the storage writes the attribute. When
        :data:`False`, the attribute is read-only.
    """

    def __init__(self, field: str, *, writable: bool = True) -> None:
        super().__init__(field, writable=writable)

    @property
    def field(self) -> str:
        """The name of the field holding the value."""
        return str(self.target)


class Computed:
    """A SCIM attribute computed by a query expression, that the storage reads and never writes.

    Filters and sorts compare the expression, so they compare the value the
    clients read. The expression only refers to fields of the model, such as
    ``Concat("first_name", Value(" "), "last_name")``.

    To compute a value in the database once for all, map a
    :class:`~django.db.models.GeneratedField` by its name instead.

    :param expression: The :doc:`query expression <django:ref/models/expressions>`
        computing the value.
    """

    def __init__(self, expression: Any) -> None:
        self.expression = expression


class Transformed:
    """A field holding a SCIM attribute in another form, such as an identifier with a prefix, or the hash of a password.

    The storage reads the value clients read through ``expression``, so that
    filters and sorts compare it in the database. It writes the value clients
    send through ``to_field``. The two must agree: ``to_field`` gives back the
    field value ``expression`` was computed from.

    Without ``expression``, the storage reads the value of the field as it is.
    Only an attribute hidden from clients allows it, write-only or never
    returned, such as a password, and the field must hold a value of the type
    of the attribute. A value sent back as the storage read it is not written
    again, so that the hash of a password is not hashed again. An attribute
    without ``to_field`` is read-only. ``id`` needs both: the
    storage finds a resource by its ``id`` through ``to_field``, so that the
    database uses the index of the field.

    :param field: The name of the field holding the value.
    :param expression: The :doc:`query expression <django:ref/models/expressions>`
        computing the value clients read from the field, such as
        ``Concat(Value("u"), Cast("id", TextField()), output_field=TextField())``.
    :param to_field: The function converting a value clients send into the
        value of the field, such as
        :func:`~django.contrib.auth.hashers.make_password`. It gets None for
        no value, and when a resource is created without value. A
        :exc:`ValueError`, a :exc:`TypeError` or a
        :exc:`~django.core.exceptions.ValidationError` it raises refuses the
        value, and so does None for a field without null.
    """

    def __init__(
        self,
        field: str,
        *,
        expression: Any = None,
        to_field: Callable[[Any], Any] | None = None,
    ) -> None:
        if expression is None and to_field is None:
            raise ValueError(f"Transformed({field!r}) needs expression or to_field")

        self.field = field
        self.expression = expression
        self.to_field = to_field


class Many(core.Many):
    """The entries of a multi-valued attribute, stored in the records of another model.

    The entries belong to the resource: writing the resource replaces them.
    The foreign key from the entries to the resource needs
    ``on_delete=models.CASCADE``, and cannot be null. The
    :attr:`~django.db.models.Options.ordering` of the model of the entries
    gives their order, or else their primary key. A sort uses the ``primary``
    entry, or else the first one.

    The entries of a multi-valued simple attribute, such as a list of strings,
    are the values of one field of the entry model.

    :param relation: The name of the reverse relation from the resource model
        to the entry model, such as the ``related_name`` of the foreign key.
    :param attributes: The names of the fields of the entry model, by
        sub-attribute name, such as ``"value"`` or ``"type"``. For a simple
        attribute, the name of the field holding the values.
    """

    def __init__(self, relation: str, attributes: Mapping[str, Any] | Any) -> None:
        super().__init__(relation, attributes)


class Entry(core.Entry):
    """A multi-valued complex attribute holding one entry at most, stored in fields of the resource model.

    Such as the email address of a user, in its ``email`` field. The
    resource holds an entry when one of the fields has a value, so the fields
    can hold no value: a write removing the entry empties them. A write with
    several entries is refused.

    :param attributes: The names of the fields of the resource model holding
        the sub-attributes, by sub-attribute name, such as
        ``{"value": "email"}``. A value may be an :class:`Attribute`.
    :param type: The ``type`` sub-attribute of the entry, such as
        ``"work"``. A client may send it, and a write with another type is
        refused. Without it, the storage does not store ``type``.
    :param primary: The ``primary`` sub-attribute of the entry. A client may
        send any value, as the lone entry is the primary one, and reads this
        one back. Without it, the storage does not store ``primary``.
    """


class Link(core.Link):
    """A complex attribute linking to other resources.

    A multi-valued attribute, such as the ``members`` of a group, maps to a
    :class:`~django.db.models.ManyToManyField`, or to its reverse relation. A
    single-valued one, such as the ``manager`` of an enterprise user, maps to
    a :class:`~django.db.models.ForeignKey` or a
    :class:`~django.db.models.OneToOneField`.

    ``value`` is the ``id`` of the linked resource. Its display name, the
    ``display`` or ``displayName`` sub-attribute, is the ``displayName`` of the
    linked resource when its mapping has one, or else its ``userName``.
    Without either, the links have no display name, although
    :meth:`ResourceMapping.schemas` publishes it. Writing the resource changes
    the links, and never creates nor deletes the
    linked resources.

    A Link links to the resources of one resource type. An attribute linking
    to several resource types, such as the ``members`` of a group holding users
    and groups, maps to a list of Links, one per resource type. The entries are
    read in the order of the list.

    :param relation: The name of the relation from the resource model to the
        model of the linked resources.
    :param resource_type: The name of the resource type of the linked
        resources, as the storage knows it.
    :param type: The ``type`` sub-attribute of the entries, such as ``"User"``
        for the members of a group, or ``"direct"`` for the groups of a user.
        A client may send it, and a write with another value is refused.
        Without it, the storage does not store ``type``.
    """


@dataclass(eq=False)
class _Column(core.Column):
    """A SCIM attribute stored in a field, or computed by an expression.

    key is the name of the field, or the alias the expression is annotated
    under. relation is the reverse one-to-one relation leading to the record
    holding the field, when the field is not one of the resource record, as in
    "scim__external_id".

    A transformed field is read through its expression, under key, and
    written through to_field, under field_key.
    """

    field: "models.Field[Any, Any] | None" = None
    expression: Any = None
    relation: Any = None
    to_field: Callable[[Any], Any] | None = None
    path: str | None = None

    @property
    def field_key(self) -> str:
        """The name of the field in lookups, which differs from key for a transformed field."""
        return self.path or self.key

    @property
    def transformed(self) -> bool:
        """Whether the field holds the values in another form than clients read and send."""
        return self.path is not None

    @property
    def compared(self) -> Any:
        """The expression filters and sorts compare.

        An identifier that is not text is compared as the text clients read.
        An empty string is no value in a text field without null.
        """
        expression = F(self.key) if self.expression is None else self.expression
        if self.textual and isinstance(self.field, models.UUIDField):
            return _UUIDText(expression)

        if self.textual:
            return Cast(expression, models.TextField())

        if self.empty_string:
            return NullIf(expression, Value(""), output_field=models.TextField())

        return expression

    def fill(self, in_record: bool) -> None:
        """Fill the facts of the storage the mapping core reads, from the field.

        A text field without null holds an empty string for no value. An
        expression is taken as nullable, and so is a transformed field, which
        holds what to_field gives for no value. A field of a related record
        needs no value, as the related record may not exist. A transformed
        field stores other values than clients send.
        """
        field = self.field
        self.related = self.relation is not None
        self.empty_string = (
            self.is_string
            and field is not None
            and not field.null
            and not self.transformed
        )
        self.nullable = (
            field is None or self.transformed or bool(field.null) or self.empty_string
        )
        self.needs_value = field is not None and _field_needs_value(field)
        self.fills_none = self.transformed
        if field is not None and not self.transformed and field.choices:
            self.enumeration = [value for value, _ in field.flatchoices]

        if in_record and field is not None and self.relation is None:
            self.stores = field.name


@dataclass(eq=False)
class _Collection(core.Collection):
    """A multi-valued attribute, or a complex one, stored in related records.

    relation is the Django relation from the resource model to the entries.
    """

    @property
    def entry_model(self) -> type[models.Model]:
        """The model of the entries."""
        model: type[models.Model] = self.relation.related_model
        return model

    @property
    def accessor(self) -> str:
        """The name of the attribute of a record holding the entries."""
        if isinstance(self.relation, ForeignObjectRel):
            return str(self.relation.get_accessor_name())

        return str(self.relation.name)

    def entries(self) -> "models.QuerySet[Any]":
        """Return the entries of the record of the outer query."""
        return self.entry_model._default_manager.filter(**self._correlation())

    def exists(self, condition: Q | None = None) -> Q:
        """Return the condition that an entry exists, and matches condition when given."""
        entries = self.entries()
        if condition is not None:
            entries = entries.filter(condition)

        return Q(Exists(entries))

    def _correlation(self) -> dict[str, Any]:
        """Return the lookup tying the entries to the record of the outer query."""
        relation: Any = self.relation
        if isinstance(relation, ForeignObjectRel):
            field: Any = relation.field
            if relation.many_to_many:
                return {field.name: OuterRef("pk")}

            return {field.attname: OuterRef(field.target_field.attname)}

        if relation.many_to_many:
            return {relation.related_query_name(): OuterRef("pk")}

        return {relation.target_field.attname: OuterRef(relation.attname)}

    def order(self) -> list[Any]:
        """Return the order of the entries: the one of their default manager, or else of their model, then their primary key.

        The primary key orders the entries an ordering leaves tied, so that
        they always come in the same order.
        """
        entries = self.entry_model._default_manager.all()
        ordering: Any = entries.query.order_by or self.entry_model._meta.ordering
        return [*ordering, "pk"]

    def prefetch(self) -> "Prefetch[Any]":
        """Return how to load the entries along with the records, in the order they are read."""
        entries = self.entry_model._default_manager.order_by(*self.order())
        target: Any = self.target
        if target is not None:
            entries = entries.annotate(**target._annotations())
            related = target._related_names()
            if related:
                entries = entries.select_related(*related)

        return Prefetch(self.accessor, queryset=entries)


class ResourceMapping(core.ResourceMapping):
    """How the resources of a SCIM model are stored in the records of a Django model.

    The paths are checked against the SCIM model when the mapping is created,
    so a mistake in a path raises at import time. The fields are checked by
    the system check framework, since the models are not all loaded at import
    time: with ``scim2_django`` in :setting:`INSTALLED_APPS`, ``manage.py
    check`` reports an unknown field, or a field that cannot hold its
    attribute. A storage also checks its mappings before its first operation.

    :param model: The SCIM model of the resources, such as
        ``User[EnterpriseUser]``.
    :param record: The Django model of the records.
    :param attributes: The attributes, by SCIM path. A value is the name of a
        field, an :class:`Attribute`, a :class:`Computed`, a
        :class:`Transformed`, a :class:`Many`, an :class:`Entry`, a
        :class:`Link`, or a list of :class:`Link`. A field of a model related
        by a reverse one-to-one relation, such as a table beside the users,
        is mapped with a path such as ``"scim__external_id"``. The storage
        creates the related record at the first value it writes there.
        ``id`` is required, and maps to a field or a :class:`Transformed`.
        ``meta.created`` and ``meta.lastModified`` are filled by the storage:
        without them, the resources have no such date.
    :param version: The name of an integer field counting the writes of each
        record, which gives ``meta.version``. A write changes the version of
        the written resource only: adding a member to a group changes the
        version of the group, not the one of the user, although the
        ``groups`` of the user change. Without it, ``meta.version`` is a hash
        of the whole resource, which any change changes. The storage then
        reads every collection of a resource, even those a response leaves
        out, such as the members of a group: prefer a version field for large
        groups. The hash protects the writes on databases that read committed
        data, as Django sets up PostgreSQL and MariaDB, and as SQLite does.
        Under repeatable reads, use a version field.
    :raises ValueError: When a path is not valid for the model, or when a
        value does not fit the attribute of its path.
    """

    configuration_error = ImproperlyConfigured

    record: type[models.Model]

    _FIELDS = ("id", "meta.created", "meta.lastModified")
    """The attributes the storage fills itself, which a field holds as it is."""

    version: str | None
    _id: _Column
    _created: _Column | None
    _last_modified: _Column | None

    def __init__(
        self,
        model: type[Resource[Any]],
        record: type[models.Model],
        attributes: Mapping[str, Any],
        *,
        version: str | None = None,
    ) -> None:
        self._messages: list[checks.CheckMessage] | None = None
        super().__init__(model, record, attributes, version=version)
        _MAPPINGS.add(self)

    def __repr__(self) -> str:
        return f"<ResourceMapping {self.model.__name__} {self.record._meta.label}>"

    def schemas(self) -> list[Schema]:
        """Return the schemas of the resources, restricted to what the mapping stores.

        They hold the mapped attributes only, and an attribute the mapping
        reads but never writes is ``readOnly``. Publish them, rather than the
        schemas of :attr:`model`, so that clients know what the server stores:
        build the provider of the server with
        :meth:`ScimProvider.from_discovery <scim2_models.ScimProvider.from_discovery>`,
        and pass it to the storage.

        An attribute is ``required`` when the storage cannot create a record
        without its value: its field has neither null nor a default, and gets
        no value by itself. A string held by a text field never makes it
        required, since Django stores an empty string. A field of a related
        record never does either. Nor does a :class:`Transformed` field: its
        ``to_field`` returns the value stored when none is given. An attribute
        never returned, such as a write-only one, is optional, even when its
        schema requires it: clients cannot send back a value they never read.
        A field with a default but without null is optional, but its value
        cannot be removed.

        A required attribute of an extension only constrains the resources
        holding that extension. Yet the storage refuses a resource without the
        extension when the field of such an attribute needs a value. Declare
        the extension required in the resource type, so that clients know.

        :returns: The schema of the resource, then the schemas of its extensions.
        :raises ~django.core.exceptions.ImproperlyConfigured: When a field does
            not fit its attribute.
        """
        self._ready()
        return super().schemas()

    def check(self) -> list[checks.CheckMessage]:
        """Return the errors and the warnings of the fields of the mapping.

        The models must be loaded. The result is computed once.
        """
        apps.check_models_ready()
        if self._messages is None:
            self._messages = [self._message(problem) for problem in self._resolve()]

        return self._messages

    def _message(self, problem: MappingProblem) -> checks.CheckMessage:
        """Return the system check message of a problem of the mapping."""
        id = _CHECK_IDS.get(problem.code, problem.code)
        if problem.level == "warning":
            return checks.Warning(problem.message, obj=self, id=id)

        return checks.Error(problem.message, obj=self, id=id)

    def _ready(self) -> None:
        """Resolve the fields, raising when one does not fit its attribute."""
        errors = [message for message in self.check() if message.is_serious()]
        if errors:
            raise ImproperlyConfigured(
                "\n".join(f"{self!r}: {error.msg}" for error in errors)
            )

    def _queryset(
        self, collections: Iterable[core.Collection], using: str = DEFAULT_DB_ALIAS
    ) -> "models.QuerySet[Any]":
        """Return the records of a database with their computed attributes, and the given collections loaded along."""
        records = self.record._default_manager.using(using)
        related = self._related_names()
        if related:
            records = records.select_related(*related)

        return records.annotate(**self._annotations()).prefetch_related(
            *(collection.prefetch() for collection in collections)  # type: ignore[attr-defined]
        )

    def _annotations(self) -> dict[str, Any]:
        """Return the expressions of the computed attributes, by the alias records hold them under."""
        return {
            column.key: column.expression  # type: ignore[attr-defined]
            for column in self._columns.values()
            if column.expression is not None  # type: ignore[attr-defined]
        }

    def _targets(
        self, mappings: Mapping[str, core.ResourceMapping]
    ) -> list[tuple[core.Collection, core.ResourceMapping]]:
        """Return each Link with the mapping of the resources it links to.

        Several storages may share a mapping, as long as they link it to the
        same mappings.
        """
        self._ready()
        return super()._targets(mappings)

    def _relations(self) -> list[Any]:
        """Return the relations leading to the records holding mapped fields, by name."""
        relations = {
            column.relation.name: column.relation  # type: ignore[attr-defined]
            for column in self._columns.values()
            if column.relation is not None  # type: ignore[attr-defined]
        }
        return [relations[name] for name in sorted(relations)]

    def _related_names(self) -> list[str]:
        """Return the names select_related takes for the relations leading to the records holding mapped fields."""
        return [relation.name for relation in self._relations()]

    def _is_target(self, value: Any) -> bool:
        return isinstance(value, str | Computed | Transformed)

    def _value_writable(self, value: Any) -> bool:
        if isinstance(value, Computed):
            return False

        if isinstance(value, Transformed):
            return value.to_field is not None

        return super()._value_writable(value)

    def _check_declared(
        self,
        path: str,
        binding: AttributeBinding,
        value: Any,
        holder: core.Many | core.Entry | None,
    ) -> None:
        """Refuse a value Django cannot map where it is declared.

        The entries of another model can be neither computed nor transformed.
        An Entry maps fields of the resource model. The attributes the storage
        fills itself map fields, but id may be transformed. Clients read the
        value of a Transformed through its expression.
        """
        if isinstance(holder, core.Many) and isinstance(value, Computed | Transformed):
            raise ValueError(
                f"{path!r} is held by the entries of another model, "
                "which cannot be computed nor transformed"
            )

        if isinstance(holder, core.Entry):
            if not isinstance(value, str | Attribute):
                raise ValueError(
                    f"{path!r} must be mapped to the name of a field, or an Attribute"
                )

            field = value if isinstance(value, str) else value.field
            if "__" in field:
                raise ValueError(
                    f"{path!r} is held by a field of the resource model, not of a "
                    "related model"
                )

        if holder is not None:
            return

        is_id = _key(binding) == _key(self._resolve_path("id"))
        if is_id and isinstance(value, Transformed):
            if value.expression is None or value.to_field is None:
                raise ValueError(
                    f"{path!r} is mapped to a Transformed, which needs an "
                    "expression and to_field"
                )

            return

        fields = {_key(self._resolve_path(path)) for path in self._FIELDS}
        if _key(binding) in fields and not isinstance(value, str):
            raise ValueError(f"{path!r} must be mapped to the name of a field")

        read = Mutability.write_only not in _mutabilities(
            binding
        ) and not _never_returned(binding)
        if read and isinstance(value, Transformed) and value.expression is None:
            raise ValueError(
                f"Clients read {path!r}, so its Transformed needs an expression, "
                "unless its schema makes it write-only"
            )

    def _error(self, message: str, id: str) -> MappingProblem:
        """Return an error about the mapping, under the identifier of its system check."""
        return MappingProblem("error", id, message)

    def _build_column(
        self,
        path: str,
        binding: AttributeBinding,
        value: Any,
        owner: core.Collection | None,
    ) -> Problems:
        """Build the column of an attribute, in the resource record or in the entries of owner."""
        model = self.record if owner is None else owner.entry_model  # type: ignore[attr-defined]
        column = yield from self._value_column(model, path, binding, value)
        if column is None:
            return None

        column.fill(in_record=owner is None)
        if owner is None and _key(binding) == _key(self._resolve_path("id")):
            column.textual = not (
                column.transformed or isinstance(column.field, _TEXT_FIELDS)
            )
            column.keyed = column.textual or column.transformed

        return column

    def _value_column(
        self,
        model: type[models.Model],
        path: str,
        binding: AttributeBinding,
        value: Any,
    ) -> Generator[MappingProblem, None, _Column | None]:
        """Build the column of a declared value, and decide whether it is written.

        A read-only attribute, a generated field and a computed expression are
        never written.
        """
        writable = Mutability.read_only not in _mutabilities(binding)
        if isinstance(value, Computed):
            alias = _alias(binding)
            output = yield from self._output_field(path, alias, value.expression)
            if output is None:
                return None

            computed = _Column(binding, alias, False, expression=value.expression)
            if computed.is_string and not _holds_text(output):
                yield self._not_text(path, "its expression")
                return None

            return computed

        if isinstance(value, Transformed):
            return (yield from self._transformed(model, path, binding, value, writable))

        attribute = value if isinstance(value, Attribute) else Attribute(value)
        column = yield from self._field_column(
            model,
            path,
            binding,
            attribute.field,
            attribute.writable and writable,
        )
        if column is None:
            return None

        field: Any = column.field
        if not column.is_string or _key(binding) == _key(self._resolve_path("id")):
            return column

        if not _holds_text(field):
            yield self._not_text(path, repr(attribute.field))
            return None

        if not column.concealed and _converts(field):
            yield MappingProblem(
                "warning",
                "scim2_django.W001",
                f"{path!r} is mapped to {attribute.field!r}, whose field converts "
                "the values it reads: filters and sorts compare the stored values, "
                "not the values clients read",
            )

        return column

    def _field(
        self, model: type[models.Model], name: str, path: str
    ) -> Generator[MappingProblem, None, Any]:
        """Return the field of a model, yielding an error when the model has none of that name."""
        try:
            field: Any = model._meta.get_field(name)
        except FieldDoesNotExist:
            yield self._error(
                f"{path!r} is mapped to {name!r}, which is not a field of "
                f"{model._meta.label}",
                "scim2_django.E001",
            )
            return None

        return field

    def _field_column(
        self,
        model: type[models.Model],
        path: str,
        binding: AttributeBinding,
        name: str,
        writable: bool,
    ) -> Generator[MappingProblem, None, _Column | None]:
        """Build the column of a field, of the record or of a record related by a reverse one-to-one relation.

        A generated field is never written.
        """
        relation, field_name = None, name
        if "__" in name:
            relation = yield from self._relation(model, path, binding, name)
            if relation is None:
                return None

            model, field_name = relation.related_model, name.partition("__")[2]

        field = yield from self._field(model, field_name, path)
        if field is None:
            return None

        if field.is_relation or not field.concrete:
            yield self._error(
                f"{path!r} is mapped to {name!r}, which is a relation: "
                "map it with Many or Link",
                "scim2_django.E002",
            )
            return None

        if field.generated and (
            Mutability.write_only in _mutabilities(binding) or _never_returned(binding)
        ):
            yield self._error(
                f"Clients never read {path!r}, and {name!r} is a generated field, "
                "which the storage never writes",
                "scim2_django.E011",
            )
            return None

        return _Column(
            binding,
            name,
            writable and not field.generated,
            field=field,
            relation=relation,
        )

    def _transformed(
        self,
        model: type[models.Model],
        path: str,
        binding: AttributeBinding,
        transformed: Transformed,
        writable: bool,
    ) -> Generator[MappingProblem, None, _Column | None]:
        """Build the column of a transformed field, read through its expression and written through to_field.

        Without expression, the field is read as it is stored.
        """
        column = yield from self._field_column(
            model,
            path,
            binding,
            transformed.field,
            writable and transformed.to_field is not None,
        )
        if column is None:
            return None

        column.path = transformed.field
        column.to_field = transformed.to_field
        field: Any = column.field
        if transformed.expression is None:
            if column.is_string and not _holds_text(field):
                yield self._not_text(path, repr(transformed.field))
                return None

            return column

        alias = _alias(binding)
        output = yield from self._output_field(path, alias, transformed.expression)
        if output is None:
            return None

        if column.is_string and not _holds_text(output):
            yield self._not_text(path, "its expression")
            return None

        column.key, column.expression = alias, transformed.expression
        return column

    def _relation(
        self, model: type[models.Model], path: str, binding: AttributeBinding, name: str
    ) -> Generator[MappingProblem, None, Any]:
        """Return the reverse one-to-one relation a field name leads through, yielding an error for any other path.

        Only a field of the resource record leads through a relation, and only
        for another attribute than id.
        """
        relation_name, _, field_name = name.partition("__")
        relation = None
        if model is self.record and _key(binding) != _key(self._resolve_path("id")):
            relation = yield from self._field(model, relation_name, path)
            if relation is None:
                return None

        if (
            not isinstance(relation, ForeignObjectRel)
            or not relation.one_to_one
            or "__" in field_name
        ):
            yield self._error(
                f"{path!r} is mapped to {name!r}: a field of another model is "
                "reached through one reverse one-to-one relation of the resource "
                "model, for another attribute than id",
                "scim2_django.E010",
            )
            return None

        return relation

    def _not_text(self, path: str, holder: str) -> MappingProblem:
        """Return the error of a string attribute held by something else than text."""
        return self._error(
            f"{path!r} is a string, but {holder} does not hold text: "
            "filters, sorts and writes would not compare or store strings",
            "scim2_django.E008",
        )

    def _output_field(
        self, path: str, alias: str, expression: Any
    ) -> Generator[MappingProblem, None, Any]:
        """Return the field an expression gives, yielding an error when the model cannot compute it."""
        try:
            query = self.record._default_manager.annotate(**{alias: expression}).query
            return query.annotations[alias].output_field
        except FieldError as error:
            yield self._error(
                f"{path!r} cannot be computed: {error}", "scim2_django.E009"
            )
            return None

    def _build_collection(
        self,
        path: str,
        binding: AttributeBinding,
        declaration: core.Many | core.Link,
    ) -> Problems:
        """Build the collection of a Many or of a Link, on the relation it names."""
        relation = yield from self._field(self.record, declaration.relation, path)
        if relation is None:
            return None

        if isinstance(declaration, core.Many):
            if not (relation.one_to_many and isinstance(relation, ForeignObjectRel)):
                yield self._error(
                    f"{path!r} needs a reverse relation from a foreign key, "
                    f"but {declaration.relation!r} is not one",
                    "scim2_django.E003",
                )
                return None

            if relation.on_delete is not CASCADE or relation.field.null:
                yield self._error(
                    f"The foreign key of {path!r} needs on_delete=CASCADE, "
                    "and cannot be null",
                    "scim2_django.E003",
                )
                return None

            return _Collection(binding, relation)

        if binding.is_multivalued:
            if not relation.many_to_many:
                yield self._link_error(path, declaration, "several entries")
                return None

            return _Collection(binding, relation)

        if not ((relation.many_to_one or relation.one_to_one) and relation.concrete):
            yield self._link_error(path, declaration, "one entry")
            return None

        return _Collection(
            binding, relation, nullable=bool(relation.null), stores=relation.name
        )

    def _link_error(self, path: str, link: core.Link, holds: str) -> MappingProblem:
        """Return the error of a Link whose relation does not fit the number of entries of its attribute."""
        expected = (
            "a foreign key or a one-to-one field"
            if holds == "one entry"
            else "a many-to-many relation"
        )
        return self._error(
            f"{path!r} holds {holds}, so it needs {expected}, but "
            f"{link.relation!r} is not one",
            "scim2_django.E003",
        )

    def _check_record(self) -> Problems:
        """Check the version, the related records, the dates and the identifier."""
        yield from self._check_version()
        yield from self._check_related()
        for path, column in (
            ("meta.created", self._created),
            ("meta.lastModified", self._last_modified),
        ):
            if column is not None and not isinstance(
                column.field, models.DateTimeField
            ):
                yield self._error(
                    f"{path!r} must be mapped to a date and time field",
                    "scim2_django.E006",
                )

        field = self._id.field
        if field is None or not (
            field.primary_key or (field.unique and not field.null)
        ):
            yield self._error(
                "'id' must be mapped to the primary key, or to a unique field "
                "without null",
                "scim2_django.E005",
            )

    def _check_version(self) -> Problems:
        """Refuse a version that is not an integer field of the record, and warn when it has no default."""
        if self.version is None:
            return

        field = yield from self._field(self.record, self.version, "version")
        if field is None:
            return

        if not isinstance(field, models.IntegerField):
            yield self._error(
                f"The version {self.version!r} must be an integer field",
                "scim2_django.E004",
            )
            return

        if _field_needs_value(field):
            yield MappingProblem(
                "warning",
                "scim2_django.W002",
                f"The field {field.name!r} needs a value, but the storage counts "
                "the writes from it, and never sets it on creation: give it a "
                "default",
            )

    def _check_related(self) -> Problems:
        """Refuse a related model that the storage cannot create from the mapped fields only."""
        mapped: dict[Any, set[str]] = {}
        for column in self._columns.values():
            if column.relation is not None:  # type: ignore[attr-defined]
                mapped.setdefault(column.relation, set()).add(column.field.name)  # type: ignore[attr-defined]

        for relation, names in mapped.items():
            for field in relation.related_model._meta.concrete_fields:
                if field is relation.field or field.name in names:
                    continue

                # Django gives an empty string to a text field without value.
                if _field_needs_value(field) and not field.empty_strings_allowed:
                    yield self._error(
                        f"{relation.get_accessor_name()!r} leads to "
                        f"{relation.related_model._meta.label} records, whose "
                        f"field {field.name!r} needs a value: map it, or give it "
                        "a default",
                        "scim2_django.E010",
                    )

        yield from self._check_dated()

    def _check_dated(self) -> Problems:
        """Refuse a related model holding a date and a mapped field that needs a value.

        The storage writes the dates on each write, so each write creates the
        related record, even when the client gives no value for that field.
        """
        dates = {_key(self._resolve_path(path)) for path in self._FIELDS}
        dated = {
            column.relation  # type: ignore[attr-defined]
            for column in self._columns.values()
            if column.relation is not None and _key(column.binding) in dates  # type: ignore[attr-defined]
        }
        for column in self._columns.values():
            if column.relation not in dated or _key(column.binding) in dates:  # type: ignore[attr-defined]
                continue

            if not column.nullable and column.needs_value:
                yield self._error(
                    f"{column.relation.get_accessor_name()!r} leads to records "  # type: ignore[attr-defined]
                    "holding dates, which each write creates, so the field of "
                    f"{column.binding.urn!r} needs null or a default",
                    "scim2_django.E010",
                )

    def _fields_needing_value(self) -> Iterable[str]:
        """Return the fields of the record a new record needs a value for, but the version.

        Django gives an empty string to a text field without value, unless
        it is unique: a second record could not hold it. The parent link of an
        inherited model is set when the parent is saved.
        """
        return [
            field.name
            for field in self.record._meta.concrete_fields
            if _field_needs_value(field)
            and not (field.empty_strings_allowed and not field.unique)
            and not (field.remote_field is not None and field.remote_field.parent_link)
            and field.name != self.version
        ]

    def _links_to(self, collection: core.Collection, target: Any) -> bool:
        target._ready()
        return bool(collection.entry_model is target.record)  # type: ignore[attr-defined]

    def _read(self, column: core.Column, record: Any) -> Any:
        """Return the SCIM value of a column of a record.

        An empty string is no value, as Django text fields store it.
        """
        value = _get(column, record)  # type: ignore[arg-type]
        if value is None or (value == "" and column.empty_string):
            return None

        return _as_scim(value)

    def _related(self, collection: core.Collection, record: Any) -> list[Any]:
        """Return the records a collection of a record holds, as a list even for a single one.

        Without an ordering of their model, the records come in the order of
        their primary key.
        """
        related = getattr(record, collection.accessor)  # type: ignore[attr-defined]
        if collection.single:
            return [] if related is None else [related]

        records = related.all()
        if not records.ordered:
            records = records.order_by("pk")

        return list(records)

    def _counter(self, record: Any) -> Any:
        return getattr(record, self.version)  # type: ignore[arg-type]

    def _as_stored(self, value: Any) -> Any:
        return _as_stored(value)

    def _holds_id(self, record: Any, value: str) -> bool:
        """Tell whether a record has an identifier.

        A record holds the expression of a transformed identifier, which gives
        the identifier clients read.
        """
        if self._id.transformed:
            return bool(getattr(record, self._id.key) == value)

        return bool(getattr(record, self._id.key) == _exact_key(self._id, value))


def _alias(binding: AttributeBinding) -> str:
    """Return the alias records hold the value of an expression under."""
    return "scim_" + binding.urn.replace(":", "_").replace(".", "_")


def _field_needs_value(field: "models.Field[Any, Any]") -> bool:
    """Tell whether a new record needs a value for a field: Django fills it neither from a default nor by itself."""
    return not (
        field.null
        or field.has_default()
        or field.has_db_default()
        or field.generated
        or getattr(field, "db_returning", False)
        or getattr(field, "auto_now", False)
        or getattr(field, "auto_now_add", False)
    )


def _holds_text(field: "models.Field[Any, Any]") -> bool:
    """Tell whether a field stores text, as a generated field may."""
    if field.generated:
        field = field.output_field  # type: ignore[attr-defined]

    return isinstance(field, _TEXT_FIELDS) or field.get_internal_type() in (
        "CharField",
        "TextField",
    )


def _converts(field: "models.Field[Any, Any]") -> bool:
    """Tell whether a field converts the values it reads from the database.

    A field whose database backend converts the values is not detected.
    """
    if field.generated:
        field = field.output_field  # type: ignore[attr-defined]

    return hasattr(field, "from_db_value")


def _as_scim(value: Any) -> Any:
    """Return a value read from the database in the form SCIM holds it.

    A date without a time zone is in UTC: the storage writes it so when USE_TZ
    is off. Binary values are encoded in base64, as the models expect them.
    """
    if isinstance(value, datetime.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)

    if isinstance(value, memoryview):
        value = bytes(value)

    if isinstance(value, bytes):
        return base64.b64encode(value).decode()

    return value


def _as_stored(value: Any) -> Any:
    """Return a SCIM value in the form a field holds it.

    Dates are in UTC, without a time zone when USE_TZ is off, since Django
    refuses aware dates then.
    """
    if isinstance(value, Enum):
        return value.value

    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        value = value.astimezone(datetime.UTC)
        return value if settings.USE_TZ else value.replace(tzinfo=None)

    return value


def _get(column: _Column, record: Any) -> Any:
    """Return the value a record holds for a column, None when the related record holding it does not exist.

    The record holds the value of an expression itself, even when the
    expression reads a related record.
    """
    if column.relation is None or column.expression is not None:
        return getattr(record, column.key)

    related = _related_record(column, record)
    return None if related is None else getattr(related, column.field.name)  # type: ignore[union-attr]


def _related_record(column: _Column, record: Any) -> Any:
    """Return the related record holding the field of a column, or None when it does not exist."""
    try:
        return getattr(record, column.relation.get_accessor_name())
    except ObjectDoesNotExist:
        return None


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


def check_mappings(**kwargs: Any) -> list[checks.CheckMessage]:
    """Check the fields of every mapping, once the URLconf has created them."""
    _load_urlconf()
    return [message for mapping in list(_MAPPINGS) for message in mapping.check()]


def _load_urlconf() -> None:
    """Import the URLconf, which usually creates the mappings through the storages it serves.

    An error is left to the URL checks of Django, which report it.
    """
    try:
        get_resolver().url_patterns  # noqa: B018
    except Exception:
        pass
