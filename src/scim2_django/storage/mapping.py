import operator
import weakref
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace
from enum import Enum
from functools import reduce
from inspect import isclass
from typing import Any

from django.apps import apps
from django.core import checks
from django.core.exceptions import FieldDoesNotExist
from django.core.exceptions import FieldError
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS
from django.db import models
from django.db.models import CASCADE
from django.db.models import Case
from django.db.models import Exists
from django.db.models import F
from django.db.models import ForeignObjectRel
from django.db.models import Func
from django.db.models import OuterRef
from django.db.models import Prefetch
from django.db.models import Q
from django.db.models import Value
from django.db.models import When
from django.db.models.functions import Cast
from django.db.models.functions import Concat
from django.db.models.functions import NullIf
from django.db.models.functions import Substr
from django.db.models.lookups import IsNull
from django.urls import get_resolver
from pydantic import BaseModel
from scim2_models import Attribute as SchemaAttribute
from scim2_models import AttributeBinding
from scim2_models import Mutability
from scim2_models import Path
from scim2_models import Required
from scim2_models import Resource
from scim2_models import ResponseParameters
from scim2_models import Schema
from scim2_models import Uniqueness

_MAPPINGS: "weakref.WeakSet[ResourceMapping]" = weakref.WeakSet()
"""The mappings created so far, which the system check validates."""

_TEXT_FIELDS = (models.CharField, models.TextField)

_UUID_PARTS = ((1, 8), (9, 4), (13, 4), (17, 4), (21, 12))


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


class _Storage(Enum):
    """How a mapping stores an attribute.

    A derived attribute is read from another resource, such as the display of
    a member, so writing it changes nothing. A ref is the $ref of a link,
    built from its value: clients may send it, and writing it changes nothing
    either. A link type is the type a Link gives its entries: clients may send
    it, and the storage checks it against the linked resource. A constant is a
    sub-attribute an Entry gives its entry: clients may send it, and the
    storage checks it against the constant.
    """

    written = "written"
    read_only = "read only"
    derived = "derived"
    ref = "ref"
    link_type = "link type"
    constant = "constant"


class Attribute:
    """A field holding a SCIM attribute that the storage only reads, or only writes.

    :param field: The name of the field holding the value.
    :param readable: Whether the storage reads the attribute. When
        :data:`False`, the attribute is write-only, such as a password: the
        storage never returns it, nor filters or sorts on it.
    :param writable: Whether the storage writes the attribute. When
        :data:`False`, the attribute is read-only.
    """

    def __init__(
        self, field: str, *, readable: bool = True, writable: bool = True
    ) -> None:
        self.field = field
        self.readable = readable
        self.writable = writable


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

    An attribute without ``expression`` is write-only, such as a password. An
    attribute without ``to_field`` is read-only. ``id`` needs both: the
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


class Many:
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
        self.relation = relation
        self.attributes = attributes


class Entry:
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

    def __init__(
        self,
        attributes: Mapping[str, str | Attribute],
        *,
        type: str | None = None,
        primary: bool | None = None,
    ) -> None:
        self.attributes = attributes
        self.constants = {
            name: value
            for name, value in (("type", type), ("primary", primary))
            if value is not None
        }


class Link:
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

    def __init__(
        self, relation: str, resource_type: str, *, type: str | None = None
    ) -> None:
        self.relation = relation
        self.resource_type = resource_type
        self.type = type


@dataclass(eq=False)
class _Column:
    """A SCIM attribute stored in a field, or computed by an expression.

    key is the name of the field, or the alias the expression is annotated
    under. textual tells whether filters compare the field as text, as they do
    for an integer identifier. keyed tells whether filters find an
    identifier by the value of its field, so that the database uses its
    index. relation is the reverse one-to-one relation leading to the record
    holding the field, when the field is not one of the resource record, as in
    "scim__external_id".

    A transformed field is read through its expression, under key, and
    written through to_field, under field_key.
    """

    binding: AttributeBinding
    key: str
    readable: bool
    writable: bool
    field: "models.Field[Any, Any] | None" = None
    expression: Any = None
    textual: bool = False
    relation: Any = None
    to_field: Callable[[Any], Any] | None = None
    path: str | None = None
    keyed: bool = False
    in_entry: bool = False

    @property
    def field_key(self) -> str:
        """The name of the field in lookups, which differs from key for a transformed field."""
        return self.path or self.key

    @property
    def transformed(self) -> bool:
        """Whether the field holds the values in another form than clients read and send."""
        return self.path is not None

    @property
    def is_string(self) -> bool:
        """Whether the attribute is a SCIM string. A binary value is not one."""
        target = self.binding.target_type
        return (
            target is not None
            and target is not bytes
            and SchemaAttribute.Type.from_python(target) == SchemaAttribute.Type.string
        )

    @property
    def empty_string(self) -> bool:
        """Whether the field holds an empty string for no value.

        Django text fields without null do so. A transformed field holds what
        to_field gives for no value.
        """
        return (
            self.is_string
            and self.field is not None
            and not self.field.null
            and not self.transformed
        )

    @property
    def nullable(self) -> bool:
        """Whether the field can hold no value.

        An expression is taken as nullable, and so is a transformed field,
        which holds what to_field gives for no value.
        """
        return (
            self.field is None
            or self.transformed
            or bool(self.field.null)
            or self.empty_string
        )

    @property
    def required(self) -> bool:
        """Whether a new record needs a value.

        It does when the field can hold no value, and gets none from a default
        or from Django, as a date with auto_now does. A field of a related
        record needs none, as the related record may not exist.
        """
        if self.field is None or self.nullable or self.relation is not None:
            return False

        return _needs_value(self.field)

    @property
    def enumeration(self) -> list[Any] | None:
        """The values a field with choices stores, or None for a field without choices.

        A transformed field stores other values than clients send.
        """
        if self.field is None or self.transformed or not self.field.choices:
            return None

        return [value for value, _ in self.field.flatchoices]

    @property
    def casefolded(self) -> bool:
        """Whether the values compare without their case."""
        return self.is_string and not self.binding.case_exact

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


@dataclass(eq=False)
class _Collection:
    """A multi-valued attribute, or a complex one, stored in related records.

    The entries of an owned collection belong to the resource. The others are
    links to the resources of another mapping, the target. A single collection
    holds one entry at most, such as the manager of a user. A simple
    collection holds the values of a multi-valued simple attribute, in the
    column it keeps under None.

    link_type is the type a Link gives its entries, and type_column the
    constant filters compare it as.
    """

    binding: AttributeBinding
    relation: "models.Field[Any, Any] | ForeignObjectRel"
    columns: dict[str | None, _Column] = dataclass_field(default_factory=dict)
    resource_type: str | None = None
    target: "ResourceMapping | None" = None
    link_type: str | None = None
    type_column: _Column | None = None

    @property
    def owned(self) -> bool:
        """Whether the entries belong to the resource, rather than link to other resources."""
        return self.resource_type is None

    @property
    def entry_model(self) -> type[models.Model]:
        """The model of the entries."""
        model: type[models.Model] = self.relation.related_model  # type: ignore[assignment]
        return model

    @property
    def single(self) -> bool:
        """Whether the collection holds one entry at most."""
        return not self.binding.is_multivalued

    @property
    def simple(self) -> bool:
        """Whether the collection holds the values of a multi-valued simple attribute."""
        return None in self.columns

    @property
    def accessor(self) -> str:
        """The name of the attribute of a record holding the entries."""
        if isinstance(self.relation, ForeignObjectRel):
            return str(self.relation.get_accessor_name())

        return self.relation.name

    def column(self, sub_field_name: str | None) -> _Column | None:
        """Return the column of a sub-attribute, or of the values of a simple collection named alone.

        A complex collection named alone has no column.
        """
        if sub_field_name == "type" and self.type_column is not None:
            return self.type_column

        return self.columns.get(sub_field_name)

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
        if self.target is not None:
            entries = entries.annotate(**self.target._annotations())
            related = self.target._related_names()
            if related:
                entries = entries.select_related(*related)

        return Prefetch(self.accessor, queryset=entries)

    @property
    def writable(self) -> bool:
        """Whether writing the resource writes these entries.

        Entries of which no sub-attribute is written are left as they are.
        """
        if self.owned and not any(column.writable for column in self.columns.values()):
            return False

        mutability = self.binding.model.get_field_annotation(
            self.binding.field_name, Mutability
        )
        return bool(mutability != Mutability.read_only)


@dataclass(eq=False)
class _Entry:
    """A multi-valued complex attribute holding one entry at most, in fields of the resource record.

    columns holds the columns of the mapped sub-attributes, and constants the
    columns of the sub-attributes the entry always has, by sub-attribute
    name. A constant column has no value when the entry does not exist.
    """

    binding: AttributeBinding
    columns: dict[str, _Column]
    constants: dict[str, tuple[Any, _Column]]
    simple = False

    @property
    def writable(self) -> bool:
        """Whether writing the resource writes the entry: its fields are all written, or all read-only."""
        return any(column.writable for column in self.columns.values())

    @property
    def present(self) -> Q:
        """The condition that the record has the entry: one of its sub-attributes has a value."""
        return reduce(
            operator.or_,
            (Q(IsNull(column.compared, False)) for column in self.columns.values()),
        )

    def exists(self, condition: Q) -> Q:
        """Return the condition that the entry exists and matches condition."""
        return self.present & condition


class ResourceMapping:
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
        data, as PostgreSQL and SQLite do. Under the repeatable reads of
        MySQL, use a version field.
    :raises ValueError: When a path is not valid for the model, or when a
        value does not fit the attribute of its path.
    """

    _REQUIRED = ("id",)
    _FIELDS = ("id", "meta.created", "meta.lastModified")
    """The attributes the storage fills itself, which a field holds as it is."""

    def __init__(
        self,
        model: type[Resource[Any]],
        record: type[models.Model],
        attributes: Mapping[str, Any],
        *,
        version: str | None = None,
    ) -> None:
        self.model = model
        self.record = record
        self.version = version
        self._declared: dict[str, tuple[AttributeBinding, Any]] = {}
        self._messages: list[checks.CheckMessage] | None = None
        self._columns: dict[tuple[type[BaseModel], str, str | None], _Column] = {}
        self._collections: dict[tuple[type[BaseModel], str], list[_Collection]] = {}
        self._entries: dict[tuple[type[BaseModel], str], _Entry] = {}

        declared: dict[tuple[Any, ...], str] = {}
        for path, value in attributes.items():
            binding = self._resolve(path)
            key = _key(binding)
            for other, other_path in declared.items():
                if _overlap(key, other):
                    raise ValueError(
                        f"{other_path!r} and {path!r} map the same attribute"
                    )

            declared[key] = path
            self._declare(path, binding, value)

        for required in self._REQUIRED:
            if _key(self._resolve(required)) not in declared:
                raise ValueError(f"The mapping of {model.__name__} needs {required!r}")

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

        :returns: The schema of the resource, then the schemas of its extensions.
        :raises ~django.core.exceptions.ImproperlyConfigured: When a field does
            not fit its attribute.
        """
        self._ready()
        models_: list[Any] = [self.model, *self.model.get_extension_models().values()]
        return [self._restrict(model.to_schema()) for model in models_]

    def check(self) -> list[checks.CheckMessage]:
        """Return the errors and the warnings of the fields of the mapping.

        The models must be loaded. The result is computed once.
        """
        apps.check_models_ready()
        if self._messages is None:
            self._messages = list(self._resolve_fields())

        return self._messages

    def _ready(self) -> None:
        """Resolve the fields, raising when one does not fit its attribute."""
        errors = [message for message in self.check() if message.is_serious()]
        if errors:
            raise ImproperlyConfigured(
                "\n".join(f"{self!r}: {error.msg}" for error in errors)
            )

    def _restrict(self, schema: Schema) -> Schema:
        """Return a schema with the attributes the mapping stores only."""
        attributes = []
        for attribute in schema.attributes or []:
            restricted = self._restrict_attribute(
                f"{schema.id}:{attribute.name}", attribute
            )
            if restricted is not None:
                attributes.append(restricted)

        return schema.model_copy(update={"attributes": attributes})

    def _restrict_attribute(self, urn: str, attribute: Any) -> Any:
        """Return an attribute of a schema as the mapping stores it, or None when it stores nothing of it."""
        if attribute.sub_attributes:
            return self._restrict_complex(urn, attribute)

        storage = self._storage(urn)
        if storage is None:
            return None

        if storage in (_Storage.written, _Storage.link_type):
            return attribute

        if storage == _Storage.constant:
            constant = self._constant_value(urn)
            if not attribute.canonical_values:
                return attribute

            return attribute.model_copy(update={"canonical_values": [constant]})

        if storage == _Storage.ref:
            linked = self._linked_resource_types(urn)
            return attribute.model_copy(update={"reference_types": linked})

        return attribute.model_copy(update={"mutability": Mutability.read_only})

    def _restrict_complex(self, urn: str, attribute: Any) -> Any:
        """Return a complex attribute with the sub-attributes the mapping stores, or None when it stores none."""
        sub_attributes = [
            restricted
            for sub_attribute in attribute.sub_attributes
            if (
                restricted := self._restrict_attribute(
                    f"{urn}.{sub_attribute.name}", sub_attribute
                )
            )
            is not None
        ]
        if not sub_attributes:
            return None

        written = any(
            sub_attribute.mutability != Mutability.read_only
            for sub_attribute in sub_attributes
        )
        mutability = attribute.mutability if written else Mutability.read_only
        return attribute.model_copy(
            update={"sub_attributes": sub_attributes, "mutability": mutability}
        )

    def _storage(self, urn: str) -> _Storage | None:
        """Return how the mapping stores the attribute of a URN, or None when it does not store it."""
        key = urn.lower()
        for column in self._columns.values():
            if column.binding.urn.lower() == key:
                return _Storage.written if column.writable else _Storage.read_only

        for collection in self._all_collections():
            if collection.owned:
                for column in collection.columns.values():
                    if column.binding.urn.lower() == key:
                        return (
                            _Storage.written if column.writable else _Storage.read_only
                        )

                continue

            head = collection.binding.urn.lower()
            if key == f"{head}.value":
                return _Storage.written

            if key in (f"{head}.display", f"{head}.displayname"):
                return _Storage.derived

            if key == f"{head}.$ref":
                return _Storage.ref

            if key == f"{head}.type" and collection.link_type is not None:
                return _Storage.link_type

        for entry in self._entries.values():
            for _, column in entry.constants.values():
                if column.binding.urn.lower() != key:
                    continue

                if entry.writable:
                    return _Storage.constant

                return _Storage.read_only

        return None

    def _constant_value(self, urn: str) -> Any:
        """Return the constant an Entry gives the sub-attribute of a URN."""
        key = urn.lower()
        return next(
            constant
            for entry in self._entries.values()
            for constant, column in entry.constants.values()
            if column.binding.urn.lower() == key
        )

    def _linked_resource_types(self, urn: str) -> list[str]:
        """Return the resource types the Links of an attribute link to, from the URN of its $ref."""
        head = urn.lower().removesuffix(".$ref")
        return [
            collection.resource_type
            for collection in self._all_collections()
            if collection.binding.urn.lower() == head
            and collection.resource_type is not None
        ]

    def _all_collections(self) -> Iterator[_Collection]:
        """Yield the collections of every attribute, each Link of an attribute being a collection."""
        for collections in self._collections.values():
            yield from collections

    def _column(self, path: str) -> _Column:
        """Return the column of the attribute at a path."""
        return self._columns[_key(self._resolve(path))]

    def _lookup(
        self, binding: AttributeBinding
    ) -> list[tuple[_Collection | None, _Column | None]]:
        """Return each collection holding an attribute, if any, with the column of the attribute in it.

        An attribute outside collections gives one pair, without collection.
        An attribute linking to several resource types gives a pair for each
        of its Links. The column is None for an attribute stored nowhere, and
        for a collection named alone, such as emails.
        """
        collections = self._collections.get((binding.model, binding.field_name))
        if collections is None:
            return [(None, self._columns.get(_key(binding)) or self._constant(binding))]

        return [
            (collection, collection.column(binding.sub_field_name))
            for collection in collections
        ]

    def _constant(self, binding: AttributeBinding) -> _Column | None:
        """Return the column of a sub-attribute an Entry gives its entry, or None."""
        entry = self._entries.get((binding.model, binding.field_name))
        if entry is None or binding.sub_field_name not in entry.constants:
            return None

        _, column = entry.constants[binding.sub_field_name]
        return column

    def _nullable(self, urn: str) -> bool:
        """Tell whether the field of an attribute can hold no value, True for an attribute without field."""
        key = urn.lower()
        return all(
            column.nullable
            for column in self._columns.values()
            if column.binding.urn.lower() == key
        )

    def _required(self, urn: str) -> bool:
        """Tell whether a new record needs a value for the attribute of a URN."""
        key = urn.lower()
        return any(
            column.required
            for column in self._columns.values()
            if column.binding.urn.lower() == key
        )

    def _sub_columns(self, binding: AttributeBinding) -> list[_Column]:
        """Return the mapped sub-attributes of a complex attribute holding a single value."""
        return [
            column
            for (model, field_name, sub_field_name), column in self._columns.items()
            if model is binding.model
            and field_name == binding.field_name
            and sub_field_name is not None
        ]

    def _string_columns(self) -> Iterator[_Column]:
        """Yield the columns holding strings, those of the collections included."""
        for column in self._columns.values():
            if column.is_string:
                yield column

        for entry in self._entries.values():
            for _, column in entry.constants.values():
                if column.is_string:
                    yield column

        for collection in self._all_collections():
            for column in collection.columns.values():
                if column.is_string:
                    yield column

            if collection.type_column is not None:
                yield collection.type_column

    def _unique_columns(self) -> Iterator[_Column]:
        """Yield the readable and written columns whose values no two resources share.

        A column never written, such as id, cannot get a value another
        resource holds. A column of an entry is left out, as a resource holds
        its values in a list.
        """
        for column in self._columns.values():
            if not (column.readable and column.writable) or column.in_entry:
                continue

            if column.binding.get_annotation(Uniqueness) in (
                Uniqueness.server,
                Uniqueness.global_,
            ):
                yield column

    def _returned_collections(
        self, parameters: ResponseParameters[Any] | None = None
    ) -> list[_Collection]:
        """Return the collections a response keeps, all of them without parameters.

        Without a version field, the version is a hash of the whole resource,
        so every collection is kept.
        """
        collections = list(self._all_collections())
        if parameters is None or self.version is None:
            return collections

        paths = Path[self.model].iter_paths(  # type: ignore[name-defined]
            include_subattributes=False,
            attributes=parameters.attributes,
            excluded_attributes=parameters.excluded_attributes,
        )
        bindings = (path.resolve() for path in paths)
        kept = {binding.urn for binding in bindings if binding is not None}
        return [
            collection for collection in collections if collection.binding.urn in kept
        ]

    def _queryset(
        self, collections: Iterable[_Collection], using: str = DEFAULT_DB_ALIAS
    ) -> "models.QuerySet[Any]":
        """Return the records of a database with their computed attributes, and the given collections loaded along."""
        records = self.record._default_manager.using(using)
        related = self._related_names()
        if related:
            records = records.select_related(*related)

        return records.annotate(**self._annotations()).prefetch_related(
            *(collection.prefetch() for collection in collections)
        )

    def _annotations(self) -> dict[str, Any]:
        """Return the expressions of the computed attributes, by the alias records hold them under."""
        return {
            column.key: column.expression
            for column in self._columns.values()
            if column.expression is not None
        }

    def _targets(
        self, mappings: Mapping[str, "ResourceMapping"]
    ) -> list[tuple[_Collection, "ResourceMapping"]]:
        """Return each Link with the mapping of the resources it links to.

        Several storages may share a mapping, as long as they link it to the
        same mappings.
        """
        self._ready()
        return [
            (collection, self._target(collection, mappings))
            for collection in self._all_collections()
            if not collection.owned
        ]

    def _bind(self, links: list[tuple[_Collection, "ResourceMapping"]]) -> None:
        """Bind each Link to the mapping :meth:`_targets` returned for it.

        A storage gets the targets of every mapping before binding any, so
        that a refused mapping binds nothing.
        """
        for collection, target in links:
            collection.target = target
            collection.columns = {}
            head = collection.binding.urn
            for binding, column in (
                (self._resolve(f"{head}.value"), target._id),
                (
                    self._find(f"{head}.display") or self._find(f"{head}.displayName"),
                    target._display(),
                ),
            ):
                if binding is None or column is None:
                    continue

                collection.columns[binding.sub_field_name] = replace(
                    column, binding=binding, writable=False
                )

    def _target(
        self, collection: _Collection, mappings: Mapping[str, "ResourceMapping"]
    ) -> "ResourceMapping":
        """Return the mapping a Link links to, refusing one that does not fit it."""
        target = mappings.get(collection.resource_type)  # type: ignore[arg-type]
        if target is None:
            raise ImproperlyConfigured(
                f"No mapping for the resource type {collection.resource_type!r}"
            )

        target._ready()
        if collection.target is not None and collection.target is not target:
            raise ImproperlyConfigured(
                f"{collection.binding.urn!r} already links to another mapping "
                f"of {collection.resource_type!r}: a mapping serves storages "
                "sharing the same mappings"
            )

        if collection.entry_model is not target.record:
            raise ImproperlyConfigured(
                f"{collection.binding.urn!r} links to "
                f"{collection.entry_model._meta.label} records, but the mapping "
                f"of {collection.resource_type!r} holds "
                f"{target.record._meta.label} records"
            )

        return target

    def _display(self) -> _Column | None:
        """Return the column a link to these resources displays."""
        for path in ("displayName", "userName"):
            binding = self._find(path)
            if binding is None:
                continue

            column = self._columns.get(_key(binding))
            if column is not None and column.readable:
                return column

        return None

    def _find(self, path: str) -> AttributeBinding | None:
        """Return the attribute at a path, or None if the model has none there."""
        return Path[self.model](path).resolve()  # type: ignore[name-defined]

    def _resolve(self, path: str) -> AttributeBinding:
        """Return the attribute at a path, refusing a path the model does not know."""
        binding = self._find(path)
        if binding is None:
            raise ValueError(f"{path!r} is not an attribute of {self.model.__name__}")

        return binding

    def _declare(self, path: str, binding: AttributeBinding, value: Any) -> None:
        """Check that the value of a path fits the SCIM attribute, and keep it until the fields are resolved."""
        target = binding.target_type
        is_complex = isclass(target) and issubclass(target, BaseModel)

        if isinstance(value, Many):
            self._check_many(path, binding, value, is_complex)
            self._declared[path] = (binding, value)
            return

        if isinstance(value, Entry):
            self._check_entry(path, binding, value, is_complex)
            self._declared[path] = (binding, value)
            return

        if isinstance(value, Link):
            value = [value]

        if isinstance(value, list) and all(isinstance(item, Link) for item in value):
            if not is_complex or binding.sub_field_name is not None:
                raise ValueError(
                    f"{path!r} is not a complex attribute of {self.model.__name__}"
                )

            if not binding.is_multivalued and len(value) > 1:
                raise ValueError(
                    f"{path!r} holds one value, so it is mapped to one Link"
                )

            self._check_links(path, value)
            self._declared[path] = (binding, value)
            return

        if binding.is_multivalued or is_complex:
            raise ValueError(
                f"{path!r} holds several values or sub-attributes: map it with Many "
                "or Link, or map its sub-attributes"
            )

        self._check_value(path, binding)
        self._check_field_value(path, value)
        if _key(binding) == _key(self._resolve("id")) and isinstance(
            value, Transformed
        ):
            if value.expression is None or value.to_field is None:
                raise ValueError(
                    f"{path!r} is mapped to a Transformed, which needs an "
                    "expression and to_field"
                )

            self._declared[path] = (binding, value)
            return

        fields = {_key(self._resolve(path)) for path in self._FIELDS}
        if _key(binding) in fields and not isinstance(value, str):
            raise ValueError(f"{path!r} must be mapped to the name of a field")

        self._declared[path] = (binding, value)

    def _check_many(
        self, path: str, binding: AttributeBinding, many: Many, is_complex: bool
    ) -> None:
        """Check that a Many fits a multi-valued attribute, and maps each sub-attribute once."""
        simple = not isinstance(many.attributes, Mapping)
        if (
            binding.sub_field_name is not None
            or not binding.is_multivalued
            or is_complex == simple
        ):
            kind = "simple" if simple else "complex"
            raise ValueError(
                f"{path!r} is not a multi-valued {kind} attribute of "
                f"{self.model.__name__}"
            )

        if simple:
            self._check_entry_value(path, many.attributes)
            return

        entries: dict[tuple[Any, ...], str] = {}
        for sub_path, sub_value in many.attributes.items():
            sub_binding = self._resolve(f"{path}.{sub_path}")
            if _key(sub_binding) in entries:
                raise ValueError(
                    f"{entries[_key(sub_binding)]!r} and "
                    f"{f'{path}.{sub_path}'!r} map the same attribute"
                )

            entries[_key(sub_binding)] = f"{path}.{sub_path}"
            self._check_value(f"{path}.{sub_path}", sub_binding)
            self._check_entry_value(f"{path}.{sub_path}", sub_value)

    def _check_entry(
        self, path: str, binding: AttributeBinding, entry: Entry, is_complex: bool
    ) -> None:
        """Check that an Entry fits a multi-valued complex attribute, maps fields of the record, and gives constants to sub-attributes it does not map."""
        if (
            binding.sub_field_name is not None
            or not binding.is_multivalued
            or not is_complex
        ):
            raise ValueError(
                f"{path!r} is not a multi-valued complex attribute of "
                f"{self.model.__name__}"
            )

        if not entry.attributes:
            raise ValueError(f"The Entry of {path!r} maps no sub-attribute")

        mapped = set()
        for sub_path, sub_value in entry.attributes.items():
            sub_binding = self._resolve(f"{path}.{sub_path}")
            self._check_value(f"{path}.{sub_path}", sub_binding)
            if not isinstance(sub_value, str | Attribute):
                raise ValueError(
                    f"{f'{path}.{sub_path}'!r} must be mapped to the name of a "
                    "field, or an Attribute"
                )

            field = sub_value if isinstance(sub_value, str) else sub_value.field
            if "__" in field:
                raise ValueError(
                    f"{f'{path}.{sub_path}'!r} is held by a field of the resource "
                    "model, not of a related model"
                )

            mapped.add(_key(sub_binding))

        for name in entry.constants:
            given = self._find(f"{path}.{name}")
            if given is None or _key(given) in mapped:
                raise ValueError(
                    f"The Entry of {path!r} gives {name!r}, which is not a "
                    "sub-attribute it leaves unmapped"
                )

    def _check_entry_value(self, path: str, value: Any) -> None:
        """Refuse a value of an entry that is not the name of a field, or an Attribute."""
        if isinstance(value, Computed | Transformed):
            raise ValueError(
                f"{path!r} is held by the entries of another model, "
                "which cannot be computed nor transformed"
            )

        self._check_field_value(path, value)

    def _check_field_value(self, path: str, value: Any) -> None:
        """Refuse a value that is not the name of a field, an Attribute, a Computed, or a Transformed."""
        if not isinstance(value, str | Attribute | Computed | Transformed):
            raise ValueError(
                f"{path!r} must be mapped to the name of a field, an Attribute, "
                "a Computed, or a Transformed"
            )

    def _check_value(self, path: str, binding: AttributeBinding) -> None:
        """Refuse a sub-attribute holding several values, which no field can hold."""
        if binding.sub_field_name is not None and binding.target_is_multivalued:
            raise ValueError(
                f"{path!r} holds several values in a complex attribute, "
                "which the storage cannot map"
            )

    def _check_links(self, path: str, links: list[Link]) -> None:
        """Check that the Links of an attribute link to distinct resource types, and give a type all or none."""
        resource_types = [link.resource_type for link in links]
        if not links or len(set(resource_types)) != len(resource_types):
            raise ValueError(
                f"{path!r} needs one Link per resource type, and at least one"
            )

        typed = {link.type is not None for link in links}
        if len(typed) > 1:
            raise ValueError(f"Either every Link of {path!r} has a type, or none")

        if typed == {True} and self._find(f"{path}.type") is None:
            raise ValueError(f"{path!r} has no type sub-attribute")

    def _resolve_fields(self) -> Iterator[checks.CheckMessage]:
        """Resolve the field of each attribute, and yield what does not fit."""
        for path, (binding, value) in self._declared.items():
            key = (binding.model, binding.field_name)
            if isinstance(value, Many):
                collection = yield from self._many(path, binding, value)
                if collection is not None:
                    self._collections[key] = [collection]
            elif isinstance(value, Entry):
                entry = yield from self._entry(path, binding, value)
                if entry is not None:
                    self._entries[key] = entry
                    for entry_column in entry.columns.values():
                        self._columns[_key(entry_column.binding)] = entry_column
            elif isinstance(value, list):
                collections = yield from self._links(path, binding, value)
                if collections is not None:
                    self._collections[key] = collections
            else:
                column = yield from self._build_column(
                    self.record, path, binding, value
                )
                if column is not None:
                    self._columns[(*key, binding.sub_field_name)] = column

        yield from self._check_version()
        yield from self._check_related()
        if not self._is_resolved():
            return

        self._id = self._column("id")
        self._id.textual = not (
            self._id.transformed or isinstance(self._id.field, _TEXT_FIELDS)
        )
        self._id.keyed = self._id.textual or self._id.transformed
        self._created = self._columns.get(_key(self._resolve("meta.created")))
        self._last_modified = self._columns.get(
            _key(self._resolve("meta.lastModified"))
        )
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

    def _is_resolved(self) -> bool:
        """Tell whether every required attribute got a column."""
        return all(
            _key(self._resolve(path)) in self._columns for path in self._REQUIRED
        )

    def _check_version(self) -> Iterator[checks.CheckMessage]:
        """Refuse a version that is not an integer field of the record."""
        if self.version is None:
            return

        field = yield from self._field(self.record, self.version, "version")
        if field is not None and not isinstance(field, models.IntegerField):
            yield self._error(
                f"The version {self.version!r} must be an integer field",
                "scim2_django.E004",
            )

    def _error(self, message: str, id: str) -> checks.Error:
        """Return an error about the mapping."""
        return checks.Error(message, obj=self, id=id)

    def _field(
        self, model: type[models.Model], name: str, path: str
    ) -> Generator[checks.CheckMessage, None, Any]:
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

    def _build_column(
        self,
        model: type[models.Model],
        path: str,
        binding: AttributeBinding,
        value: Any,
    ) -> Generator[checks.CheckMessage, None, _Column | None]:
        """Build the column of an attribute, and decide whether it is read and written.

        A write-only attribute is never read. A read-only attribute, a
        generated field and a computed expression are never written.
        """
        mutabilities = {
            binding.model.get_field_annotation(binding.field_name, Mutability),
            binding.get_annotation(Mutability),
        }
        readable = Mutability.write_only not in mutabilities
        writable = Mutability.read_only not in mutabilities
        if isinstance(value, Computed):
            alias = _alias(binding)
            output = yield from self._output_field(path, alias, value.expression)
            if output is None:
                return None

            computed = _Column(binding, alias, True, False, expression=value.expression)
            if computed.is_string and not _holds_text(output):
                yield self._not_text(path, "its expression")
                return None

            return computed

        if isinstance(value, Transformed):
            return (
                yield from self._transformed(
                    model, path, binding, value, readable, writable
                )
            )

        attribute = value if isinstance(value, Attribute) else Attribute(value)
        column = yield from self._field_column(
            model,
            path,
            binding,
            attribute.field,
            attribute.readable and readable,
            attribute.writable and writable,
        )
        if column is None:
            return None

        field: Any = column.field
        if not column.is_string or _key(binding) == _key(self._resolve("id")):
            return column

        if not _holds_text(field):
            yield self._not_text(path, repr(attribute.field))
            return None

        if column.readable and _converts(field):
            yield checks.Warning(
                f"{path!r} is mapped to {attribute.field!r}, whose field converts "
                "the values it reads: filters and sorts compare the stored values, "
                "not the values clients read",
                obj=self,
                id="scim2_django.W001",
            )

        return column

    def _field_column(
        self,
        model: type[models.Model],
        path: str,
        binding: AttributeBinding,
        name: str,
        readable: bool,
        writable: bool,
    ) -> Generator[checks.CheckMessage, None, _Column | None]:
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

        return _Column(
            binding,
            name,
            readable,
            writable and not field.generated,
            field,
            relation=relation,
        )

    def _transformed(
        self,
        model: type[models.Model],
        path: str,
        binding: AttributeBinding,
        transformed: Transformed,
        readable: bool,
        writable: bool,
    ) -> Generator[checks.CheckMessage, None, _Column | None]:
        """Build the column of a transformed field, read through its expression and written through to_field."""
        column = yield from self._field_column(
            model,
            path,
            binding,
            transformed.field,
            readable and transformed.expression is not None,
            writable and transformed.to_field is not None,
        )
        if column is None:
            return None

        column.path = transformed.field
        column.to_field = transformed.to_field
        if not column.readable:
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
    ) -> Generator[checks.CheckMessage, None, Any]:
        """Return the reverse one-to-one relation a field name leads through, yielding an error for any other path.

        Only a field of the resource record leads through a relation, and only
        for another attribute than id.
        """
        relation_name, _, field_name = name.partition("__")
        relation = None
        if model is self.record and _key(binding) != _key(self._resolve("id")):
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

    def _check_related(self) -> Iterator[checks.CheckMessage]:
        """Refuse a related model that the storage cannot create from the mapped fields only."""
        mapped: dict[Any, set[str]] = {}
        for column in self._columns.values():
            if column.relation is not None:
                mapped.setdefault(column.relation, set()).add(column.field.name)  # type: ignore[union-attr]

        for relation, names in mapped.items():
            for field in relation.related_model._meta.concrete_fields:
                if field is relation.field or field.name in names:
                    continue

                # Django gives an empty string to a text field without value.
                if _needs_value(field) and not field.empty_strings_allowed:
                    yield self._error(
                        f"{relation.get_accessor_name()!r} leads to "
                        f"{relation.related_model._meta.label} records, whose "
                        f"field {field.name!r} needs a value: map it, or give it "
                        "a default",
                        "scim2_django.E010",
                    )

        yield from self._check_dated()

    def _check_dated(self) -> Iterator[checks.CheckMessage]:
        """Refuse a related model holding a date and a mapped field that needs a value.

        The storage writes the dates on each write, so each write creates the
        related record, even when the client gives no value for that field.
        """
        dates = {_key(self._resolve(path)) for path in self._FIELDS}
        dated = {
            column.relation
            for column in self._columns.values()
            if column.relation is not None and _key(column.binding) in dates
        }
        for column in self._columns.values():
            if column.relation not in dated or _key(column.binding) in dates:
                continue

            if not column.nullable and _needs_value(column.field):  # type: ignore[arg-type]
                yield self._error(
                    f"{column.relation.get_accessor_name()!r} leads to records "
                    "holding dates, which each write creates, so the field of "
                    f"{column.binding.urn!r} needs null or a default",
                    "scim2_django.E010",
                )

    def _relations(self) -> list[Any]:
        """Return the relations leading to the records holding mapped fields, by name."""
        relations = {
            column.relation.name: column.relation
            for column in self._columns.values()
            if column.relation is not None
        }
        return [relations[name] for name in sorted(relations)]

    def _related_names(self) -> list[str]:
        """Return the names select_related takes for the relations leading to the records holding mapped fields."""
        return [relation.name for relation in self._relations()]

    def _not_text(self, path: str, holder: str) -> checks.Error:
        """Return the error of a string attribute held by something else than text."""
        return self._error(
            f"{path!r} is a string, but {holder} does not hold text: "
            "filters, sorts and writes would not compare or store strings",
            "scim2_django.E008",
        )

    def _output_field(
        self, path: str, alias: str, expression: Any
    ) -> Generator[checks.CheckMessage, None, Any]:
        """Return the field an expression gives, yielding an error when the model cannot compute it."""
        try:
            query = self.record._default_manager.annotate(**{alias: expression}).query
            return query.annotations[alias].output_field
        except FieldError as error:
            yield self._error(
                f"{path!r} cannot be computed: {error}", "scim2_django.E009"
            )
            return None

    def _many(
        self, path: str, binding: AttributeBinding, many: Many
    ) -> Generator[checks.CheckMessage, None, _Collection | None]:
        """Build the collection of a Many, with the columns of its entries, or the column of its values."""
        relation = yield from self._field(self.record, many.relation, path)
        if relation is None:
            return None

        if not (relation.one_to_many and isinstance(relation, ForeignObjectRel)):
            yield self._error(
                f"{path!r} needs a reverse relation from a foreign key, "
                f"but {many.relation!r} is not one",
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

        collection = _Collection(binding, relation)
        entry_model = collection.entry_model
        if not isinstance(many.attributes, Mapping):
            column = yield from self._build_column(
                entry_model, path, binding, many.attributes
            )
            if column is None:
                return None

            collection.columns[None] = column
            yield from self._check_entries(path, collection)
            return collection

        for sub_path, name in many.attributes.items():
            sub_binding = self._resolve(f"{path}.{sub_path}")
            column = yield from self._build_column(
                entry_model, f"{path}.{sub_path}", sub_binding, name
            )
            if column is None:
                return None

            collection.columns[sub_binding.sub_field_name] = column

        yield from self._check_entries(path, collection)
        return collection

    def _entry(
        self, path: str, binding: AttributeBinding, entry: Entry
    ) -> Generator[checks.CheckMessage, None, _Entry | None]:
        """Build the columns of an Entry: those of its fields, and those of its constants.

        A constant has a value when the entry exists only.
        """
        columns: dict[str, _Column] = {}
        for sub_path, value in entry.attributes.items():
            sub_binding = self._resolve(f"{path}.{sub_path}")
            column = yield from self._build_column(
                self.record, f"{path}.{sub_path}", sub_binding, value
            )
            if column is None:
                return None

            columns[sub_binding.sub_field_name] = column  # type: ignore[index]

        for column in columns.values():
            column.in_entry = True

        values = columns.values()
        if not all(column.readable and column.nullable for column in values):
            yield self._error(
                f"The entry of {path!r} exists when one of its fields has a "
                "value, and a write removing it empties them, so they cannot "
                "be write-only nor without null",
                "scim2_django.E007",
            )
            return None

        writable = [column.writable for column in values]
        if any(writable) and not all(writable):
            yield self._error(
                f"A write removing the entry of {path!r} empties its fields, so "
                "they cannot hold a read-only value next to written ones",
                "scim2_django.E007",
            )
            return None

        result = _Entry(binding, columns, {})
        for name, constant in entry.constants.items():
            sub_binding = self._resolve(f"{path}.{name}")
            expression = Case(
                When(result.present, then=Value(constant)),
                default=Value(None),
                output_field=(
                    models.BooleanField()
                    if isinstance(constant, bool)
                    else models.TextField()
                ),
            )
            result.constants[name] = (
                constant,
                _Column(sub_binding, name, True, False, expression=expression),
            )

        return result

    def _check_entries(
        self, path: str, collection: _Collection
    ) -> Iterator[checks.CheckMessage]:
        """Refuse entries that a write would partly lose.

        A write replaces the entries, so the values a client never sends back
        would be lost: those of a write-only sub-attribute, and those of a
        read-only one next to written ones. A PATCH adding entries would lose
        the write-only ones, since the server patches the resource it read.
        """
        columns = collection.columns.values()
        if not all(column.readable for column in columns):
            yield self._error(
                f"The entries of {path!r} are replaced on each write, "
                "so they cannot hold write-only values",
                "scim2_django.E007",
            )
        elif collection.writable and not all(column.writable for column in columns):
            yield self._error(
                f"The entries of {path!r} are replaced on each write, "
                "so they cannot hold a read-only value next to written ones",
                "scim2_django.E007",
            )

    def _links(
        self, path: str, binding: AttributeBinding, links: list[Link]
    ) -> Generator[checks.CheckMessage, None, list[_Collection] | None]:
        """Build the collections of the Links of an attribute, one per resource type.

        The type of the entries is compared, in filters, as a constant of each
        Link.
        """
        collections: list[_Collection] = []
        for link in links:
            relation = yield from self._field(self.record, link.relation, path)
            if relation is None:
                return None

            collection = _Collection(binding, relation)
            fits = relation.many_to_many
            if collection.single:
                fits = (
                    relation.many_to_one or relation.one_to_one
                ) and relation.concrete

            if not fits:
                expected = (
                    "a foreign key or a one-to-one field"
                    if collection.single
                    else "a many-to-many relation"
                )
                yield self._error(
                    f"{path!r} holds {'one entry' if collection.single else 'several entries'}, "
                    f"so it needs {expected}, but {link.relation!r} is not one",
                    "scim2_django.E003",
                )
                return None

            if collection.single and not relation.null and not _required(binding):
                yield self._error(
                    f"{path!r} can be removed, so {link.relation!r} needs null=True",
                    "scim2_django.E003",
                )
                return None

            collection.resource_type = link.resource_type
            if link.type is not None:
                collection.link_type = link.type
                collection.type_column = _Column(
                    self._resolve(f"{path}.type"),
                    "type",
                    True,
                    False,
                    expression=models.Value(link.type),
                )

            collections.append(collection)

        return collections


def _alias(binding: AttributeBinding) -> str:
    """Return the alias records hold the value of an expression under."""
    return "scim_" + binding.urn.replace(":", "_").replace(".", "_")


def _key(binding: AttributeBinding) -> tuple[Any, ...]:
    """Return what tells an attribute apart, whatever the case of its path."""
    return (binding.model, binding.field_name, binding.sub_field_name)


def _overlap(key: tuple[Any, ...], other: tuple[Any, ...]) -> bool:
    """Tell whether two attributes are the same, or one holds the other."""
    return key == other or (key[:2] == other[:2] and None in (key[2], other[2]))


def _required(binding: AttributeBinding) -> bool:
    """Tell whether a resource always holds an attribute."""
    return Required.true in (
        binding.model.get_field_annotation(binding.field_name, Required),
        binding.get_annotation(Required),
    )


def _needs_value(field: "models.Field[Any, Any]") -> bool:
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
