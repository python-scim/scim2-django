import weakref
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from inspect import isclass
from typing import Any

from django.apps import apps
from django.core import checks
from django.core.exceptions import FieldDoesNotExist
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import CASCADE
from django.db.models import ForeignObjectRel
from django.urls import get_resolver
from pydantic import BaseModel
from scim2_models import Attribute as SchemaAttribute
from scim2_models import AttributeBinding
from scim2_models import Mutability
from scim2_models import Path
from scim2_models import Required
from scim2_models import Resource
from scim2_models import Schema

_MAPPINGS: "weakref.WeakSet[ResourceMapping]" = weakref.WeakSet()
"""The mappings created so far, which the system check validates."""

_TEXT_FIELDS = (models.CharField, models.TextField)


class _Storage(Enum):
    """How a mapping stores an attribute.

    A derived attribute is read from another resource, such as the display of
    a member, so writing it changes nothing. A ref is the $ref of a link,
    built from its value: clients may send it, and writing it changes nothing
    either. A link type is the type a Link gives its entries: clients may send
    it, and the storage checks it against the linked resource.
    """

    written = "written"
    read_only = "read only"
    derived = "derived"
    ref = "ref"
    link_type = "link type"


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
    for an integer identifier.
    """

    binding: AttributeBinding
    key: str
    readable: bool
    writable: bool
    field: "models.Field[Any, Any] | None" = None
    expression: Any = None
    textual: bool = False

    @property
    def is_string(self) -> bool:
        """Whether the attribute is a SCIM string."""
        target = self.binding.target_type
        return (
            target is not None
            and SchemaAttribute.Type.from_python(target) == SchemaAttribute.Type.string
        )


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
        field, an :class:`Attribute`, a :class:`Computed`, a :class:`Many`, a
        :class:`Link`, or a list of :class:`Link`. ``id``, ``meta.created``
        and ``meta.lastModified`` are required.
    :param version: The name of an integer field counting the writes of each
        record, which gives ``meta.version``.
    :raises ValueError: When a path is not valid for the model, or when a
        value does not fit the attribute of its path.
    """

    _REQUIRED = ("id", "meta.created", "meta.lastModified")

    def __init__(
        self,
        model: type[Resource[Any]],
        record: type[models.Model],
        attributes: Mapping[str, Any],
        *,
        version: str,
    ) -> None:
        self.model = model
        self.record = record
        self.version = version
        self._declared: dict[str, tuple[AttributeBinding, Any]] = {}
        self._messages: list[checks.CheckMessage] | None = None
        self._columns: dict[tuple[type[BaseModel], str, str | None], _Column] = {}
        self._collections: dict[tuple[type[BaseModel], str], list[_Collection]] = {}

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
            return attribute.model_copy(
                update={
                    "sub_attributes": sub_attributes,
                    "mutability": attribute.mutability
                    if written
                    else Mutability.read_only,
                }
            )

        storage = self._storage(urn)
        if storage is None:
            return None
        if storage in (_Storage.written, _Storage.link_type):
            return attribute
        if storage == _Storage.ref:
            linked = self._linked_resource_types(urn)
            return attribute.model_copy(update={"reference_types": linked})
        return attribute.model_copy(update={"mutability": Mutability.read_only})

    def _storage(self, urn: str) -> _Storage | None:
        """Return how the mapping stores the attribute of a URN, or None when it does not store it."""
        key = urn.lower()
        for column in self._columns.values():
            if column.binding.urn.lower() == key:
                return _Storage.written if column.writable else _Storage.read_only
        for collection in self._all_collections():
            if not collection.owned:
                head = collection.binding.urn.lower()
                if key == f"{head}.value":
                    return _Storage.written
                if key in (f"{head}.display", f"{head}.displayname"):
                    return _Storage.derived
                if key == f"{head}.$ref":
                    return _Storage.ref
                if key == f"{head}.type" and collection.link_type is not None:
                    return _Storage.link_type
                continue
            for column in collection.columns.values():
                if column.binding.urn.lower() == key:
                    return _Storage.written if column.writable else _Storage.read_only
        return None

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
                collection.columns[binding.sub_field_name] = _Column(
                    binding,
                    column.key,
                    column.readable,
                    False,
                    column.field,
                    column.expression,
                    column.textual,
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
            simple = not isinstance(value.attributes, Mapping)
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
                self._check_field_value(path, value.attributes)
            else:
                entries: dict[tuple[Any, ...], str] = {}
                for sub_path, sub_value in value.attributes.items():
                    sub_binding = self._resolve(f"{path}.{sub_path}")
                    if _key(sub_binding) in entries:
                        raise ValueError(
                            f"{entries[_key(sub_binding)]!r} and "
                            f"{f'{path}.{sub_path}'!r} map the same attribute"
                        )
                    entries[_key(sub_binding)] = f"{path}.{sub_path}"
                    self._check_value(f"{path}.{sub_path}", sub_binding)
                    self._check_field_value(f"{path}.{sub_path}", sub_value)
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
        required = {_key(self._resolve(path)) for path in self._REQUIRED}
        if _key(binding) in required and not isinstance(value, str):
            raise ValueError(f"{path!r} must be mapped to the name of a field")
        self._declared[path] = (binding, value)

    def _check_field_value(self, path: str, value: Any) -> None:
        """Refuse a value that is not the name of a field, an Attribute, or a Computed."""
        if not isinstance(value, str | Attribute | Computed):
            raise ValueError(
                f"{path!r} must be mapped to the name of a field, an Attribute, "
                "or a Computed"
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
        if not self._is_resolved():
            return
        self._id = self._column("id")
        self._id.textual = not isinstance(self._id.field, _TEXT_FIELDS)
        self._created = self._column("meta.created")
        self._last_modified = self._column("meta.lastModified")
        for path, column in (
            ("meta.created", self._created),
            ("meta.lastModified", self._last_modified),
        ):
            if not isinstance(column.field, models.DateTimeField):
                yield self._error(
                    f"{path!r} must be mapped to a date and time field",
                    "scim2_django.E006",
                )
        if self._id.field is None or not (
            self._id.field.primary_key or self._id.field.unique
        ):
            yield self._error(
                "'id' must be mapped to the primary key, or to a unique field",
                "scim2_django.E005",
            )

    def _is_resolved(self) -> bool:
        """Tell whether every required attribute got a column."""
        return all(
            _key(self._resolve(path)) in self._columns for path in self._REQUIRED
        )

    def _check_version(self) -> Iterator[checks.CheckMessage]:
        """Refuse a version that is not an integer field of the record."""
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
        """Build the column of an attribute, and decide whether it is written.

        A read-only attribute, a generated field and a computed expression are
        never written.
        """
        mutabilities = {
            binding.model.get_field_annotation(binding.field_name, Mutability),
            binding.get_annotation(Mutability),
        }
        writable = Mutability.read_only not in mutabilities
        if isinstance(value, Computed):
            alias = "scim_" + binding.urn.replace(":", "_").replace(".", "_")
            return _Column(binding, alias, True, False, expression=value.expression)

        attribute = value if isinstance(value, Attribute) else Attribute(value)
        field = yield from self._field(model, attribute.field, path)
        if field is None:
            return None
        if field.is_relation or not field.concrete:
            yield self._error(
                f"{path!r} is mapped to {attribute.field!r}, which is a relation: "
                "map it with Many or Link",
                "scim2_django.E002",
            )
            return None
        column = _Column(
            binding,
            field.name,
            attribute.readable,
            attribute.writable and writable and not field.generated,
            field,
        )
        if (
            column.is_string
            and column.readable
            and _key(binding) != _key(self._resolve("id"))
            and not _holds_text(field)
        ):
            yield checks.Warning(
                f"{path!r} is mapped to {attribute.field!r}, whose field does not "
                "store the text clients read: filters and sorts compare the stored "
                "values",
                obj=self,
                id="scim2_django.W001",
            )
        return column

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
            return collection
        for sub_path, name in many.attributes.items():
            sub_binding = self._resolve(f"{path}.{sub_path}")
            column = yield from self._build_column(
                entry_model, f"{path}.{sub_path}", sub_binding, name
            )
            if column is None:
                return None
            collection.columns[sub_binding.sub_field_name] = column
        return collection

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
                if fits and not relation.null and not _required(binding):
                    yield self._error(
                        f"{path!r} can be removed, so {link.relation!r} needs "
                        "null=True",
                        "scim2_django.E003",
                    )
                    return None
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


def _holds_text(field: "models.Field[Any, Any]") -> bool:
    """Tell whether a field stores text as it reads it.

    A field converting the values it reads, such as a JSON field, does not.
    Neither does a field whose database backend converts the values, which
    this does not detect.
    """
    if field.generated:
        field = field.output_field  # type: ignore[attr-defined]
    return isinstance(field, _TEXT_FIELDS) and not hasattr(field, "from_db_value")


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
