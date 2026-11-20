import base64
import datetime
from collections.abc import Iterable
from collections.abc import Mapping
from enum import Enum
from inspect import isclass
from typing import Any

from django.conf import settings
from django.db import models
from pydantic import BaseModel
from scim2_models import AttributeBinding
from scim2_models import InvalidValueException
from scim2_models import Meta
from scim2_models import Mutability
from scim2_models import MutabilityException
from scim2_models import Path
from scim2_models import Resource

from .mapping import ResourceMapping
from .mapping import _Collection
from .mapping import _Column
from .mapping import _Storage


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


def _read(column: _Column, record: Any) -> Any:
    """Return the SCIM value of a column of a record, or None if the column is not read.

    An empty string is no value, as Django text fields store it.
    """
    value = getattr(record, column.key)
    if value is None or not column.readable or (value == "" and column.empty_string):
        return None

    return _as_scim(value)


def _version(mapping: ResourceMapping, record: Any) -> str:
    """Return the weak ETag of the version of a record."""
    return f'W/"{getattr(record, mapping.version)}"'


def _to_scim(
    mapping: ResourceMapping,
    record: Any,
    resource_type: str,
    model: type[Resource[Any]],
    endpoints: Mapping[str, str],
    collections: Iterable[_Collection],
) -> Resource[Any]:
    """Build the resource a record stores, as an instance of model.

    Only the given collections are read: the others may not be loaded.

    The attributes are found by their URN, so model may be another model of
    the same schemas as the model of the mapping, such as a model a provider
    built from the schemas. endpoints gives the endpoint of each resource type
    by name: a link to one of them gets a $ref relative to the SCIM root, when
    model declares it, which the server makes absolute (RFC 7643 §2.3.7).
    """
    resource = model()
    special = (mapping._id, mapping._created, mapping._last_modified)
    for column in mapping._columns.values():
        if column in special:
            continue

        value = _read(column, record)
        if value is not None:
            Path[model](column.binding.urn).set(resource, value)  # type: ignore[valid-type]

    read: dict[str, tuple[bool, list[Any]]] = {}
    for collection in collections:
        endpoint = None
        if Path[model](f"{collection.binding.urn}.$ref").resolve() is not None:  # type: ignore[valid-type]
            endpoint = endpoints.get(collection.resource_type)  # type: ignore[arg-type]

        _, entries = read.setdefault(collection.binding.urn, (collection.single, []))
        for related in _related(collection, record):
            entry = _read_entry(collection, related, endpoint)
            if entry is not None:
                entries.append(entry)

    for urn, (single, entries) in read.items():
        if entries:
            Path[model](urn).set(resource, entries[0] if single else entries)  # type: ignore[valid-type]

    resource.id = str(_read(mapping._id, record))
    resource.meta = Meta(
        resource_type=resource_type,
        created=_read(mapping._created, record),
        last_modified=_read(mapping._last_modified, record),
        version=_version(mapping, record),
    )
    return model.model_validate(resource.model_dump())


def _related(collection: _Collection, record: Any) -> list[Any]:
    """Return the records a collection of a record holds, as a list even for a single one.

    Without an ordering of their model, the records come in the order of
    their primary key.
    """
    related = getattr(record, collection.accessor)
    if collection.single:
        return [] if related is None else [related]

    records = related.all()
    if not records.ordered:
        records = records.order_by("pk")

    return list(records)


def _entries(collection: _Collection, resource: Resource[Any]) -> list[Any]:
    """Return the entries a resource holds for a collection, as a list even for a single one."""
    value = Path(collection.binding.urn).get(resource, strict=False)
    if collection.single:
        return [] if value is None else [value]

    return list(value or [])


def _read_entry(collection: _Collection, entry: Any, endpoint: str | None) -> Any:
    """Return the values of an entry, by sub-attribute name, or the value of an entry of a simple collection.

    A link gets a $ref when the endpoint of the resources it links to is known.
    """
    if collection.simple:
        return _read(collection.columns[None], entry)

    values = {
        column.binding.urn.rsplit(".", 1)[-1]: _read(column, entry)
        for column in collection.columns.values()
    }
    if collection.owned:
        return values

    values["value"] = str(values["value"])
    if endpoint is not None:
        values["$ref"] = f"{endpoint}/{values['value']}"

    if collection.link_type is not None:
        values["type"] = collection.link_type

    return values


def _from_scim(
    mapping: ResourceMapping,
    resource: Resource[Any],
    record: Any,
    links: dict[_Collection, list[Any]],
    creating: bool,
) -> None:
    """Write the fields of a resource into a record, before the record is saved.

    Read-only attributes are left as they are, and so is a write-only attribute
    the resource has no value for, since a resource never carries it back.
    When creating the record, a missing value is not written, so that the
    field keeps its default. A Link holding one entry is a foreign key of the
    record, so it is written here too. links holds the records each Link of
    the resource links to.
    """
    for column in mapping._columns.values():
        if not column.writable:
            continue

        value = Path(column.binding.urn).get(resource, strict=False)
        if value is None and (creating or not column.readable):
            continue

        setattr(record, column.key, _stored(column, value))

    for collection in _written(mapping):
        if collection.single and not collection.owned:
            setattr(record, collection.accessor, next(iter(links[collection]), None))


def _stored(column: _Column, value: Any) -> Any:
    """Return the value a field stores for a SCIM value, an empty string for no value in a text field without null."""
    if value is None:
        return "" if column.empty_string else None

    return _as_stored(value)


def _write_collections(
    mapping: ResourceMapping,
    resource: Resource[Any],
    record: Any,
    links: dict[_Collection, list[Any]],
) -> None:
    """Write the entries and the links of a resource, once its record is saved.

    The entries of a Many replace the former ones, which are deleted. The
    links of a Link holding several entries replace the former links.
    """
    for collection in _written(mapping):
        if collection.owned:
            getattr(record, collection.accessor).all().delete()
            entries = Path(collection.binding.urn).get(resource, strict=False) or []
            entry_records = collection.entry_model._base_manager.using(record._state.db)
            entry_records.bulk_create(
                [_write_entry(collection, entry, record) for entry in entries]
            )
        elif not collection.single:
            getattr(record, collection.accessor).set(links[collection])


def _written(mapping: ResourceMapping) -> Iterable[_Collection]:
    """Yield the collections that writing a resource changes."""
    return (
        collection for collection in mapping._all_collections() if collection.writable
    )


def _write_entry(collection: _Collection, entry: Any, record: Any) -> models.Model:
    """Build the record storing an entry of a multi-valued attribute, for a resource record."""
    owner = collection.relation.field.name  # type: ignore[union-attr]
    entry_record = collection.entry_model(**{owner: record})
    if collection.simple:
        setattr(entry_record, collection.columns[None].key, _as_stored(entry))
        return entry_record

    for column in collection.columns.values():
        value = getattr(entry, column.binding.target_field_name, None)
        if value is not None:
            setattr(entry_record, column.key, _as_stored(value))

    return entry_record


def _link_entries(
    mapping: ResourceMapping, resource: Resource[Any]
) -> list[tuple[list[_Collection], list[Any]]]:
    """Return the entries of each written attribute of a resource linking to other resources, with the collections of its Links.

    A link is stored by its value, so an entry without one, such as an entry
    with a $ref only, raises InvalidValueException rather than being lost.
    """
    links = []
    for collections in mapping._collections.values():
        collection = collections[0]
        if collection.owned or not collection.writable:
            continue

        entries = _entries(collection, resource)
        if any(entry.value is None for entry in entries):
            raise InvalidValueException(
                detail=f"'{collection.binding.urn}' holds an entry without value"
            )

        links.append((collections, entries))

    return links


def _check_storable(
    mapping: ResourceMapping,
    resource: Resource[Any],
    stored: Resource[Any] | None,
) -> None:
    """Refuse a change of a resource that the mapping cannot store.

    Each attribute of the resource is compared with the stored resource, None
    for a new one. A value equal to the stored one changes nothing, so a
    resource read and sent back is accepted. A missing value changes nothing
    either, except for a stored value whose field cannot hold none: removing it
    raises MutabilityException. The read-only attributes of the model of the
    resource are skipped, as RFC 7644 §3.5.1 has them ignored, and so are the
    derived ones.
    """
    _check_entry_values(mapping, resource)
    model = type(resource)
    for path in Path[model].iter_paths():  # type: ignore[valid-type]
        binding = path.resolve()
        if binding is None:
            continue

        target = binding.target_type
        if isclass(target) and issubclass(target, BaseModel):
            continue

        if Mutability.read_only in (
            binding.model.get_field_annotation(binding.field_name, Mutability),
            binding.get_annotation(Mutability),
        ):
            continue

        storage = mapping._storage(binding.urn)
        value = _comparable(path.get(resource, strict=False))
        current = None
        if stored is not None:
            current = _comparable(Path(binding.urn).get(stored, strict=False))

        if storage == _Storage.written:
            _check_written(mapping, binding, value, current, stored is None)
            continue

        if storage in (_Storage.derived, _Storage.ref, _Storage.link_type) or value in (
            None,
            current,
        ):
            continue

        if storage == _Storage.read_only:
            raise MutabilityException(
                attribute=binding.urn,
                mutability="readOnly",
                detail=f"'{binding.urn}' is read-only on this server",
            )

        raise InvalidValueException(
            detail=f"'{binding.urn}' is not stored by this server"
        )


def _check_written(
    mapping: ResourceMapping,
    binding: AttributeBinding,
    value: Any,
    current: Any,
    creating: bool,
) -> None:
    """Refuse a value of a written attribute that its field cannot store."""
    _check_enumerated(mapping, binding, value)
    if value is not None:
        return

    if creating and mapping._required(binding.urn):
        raise InvalidValueException(
            detail=f"'{binding.urn}' is required on this server"
        )

    if current is not None and not mapping._nullable(binding.urn):
        raise MutabilityException(
            attribute=binding.urn,
            detail=f"'{binding.urn}' cannot be removed on this server",
        )


def _check_entry_values(mapping: ResourceMapping, resource: Resource[Any]) -> None:
    """Refuse an entry without a value its field cannot do without."""
    for collection in mapping._all_collections():
        if not collection.owned or not collection.writable or collection.simple:
            continue

        for entry in _entries(collection, resource):
            for column in collection.columns.values():
                value = getattr(entry, column.binding.target_field_name, None)
                if value is None and column.required:
                    raise InvalidValueException(
                        detail=f"'{column.binding.urn}' is required on this server"
                    )


def _check_enumerated(
    mapping: ResourceMapping, binding: AttributeBinding, value: Any
) -> None:
    """Refuse a value that a field with choices does not store, rather than storing it anyway."""
    _, column = mapping._lookup(binding)[0]
    if column is None or column.enumeration is None:
        return

    stored = column.enumeration
    for item in value if isinstance(value, list) else [value]:
        if item is not None and item not in stored:
            raise InvalidValueException(
                detail=f"'{binding.urn}' cannot hold {item!r} on this server"
            )


def _comparable(value: Any) -> Any:
    """Return a value in a form two resources compare in.

    A list of missing values, as a path through the entries of a multi-valued
    attribute gives, is no value.
    """
    if isinstance(value, list):
        values = [_as_stored(item) for item in value]
        return None if all(item is None for item in values) else values

    return _as_stored(value)
