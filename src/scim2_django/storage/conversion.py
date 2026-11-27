import hashlib
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.encoding import force_bytes
from scim2_models import InvalidValueException
from scim2_models import Path
from scim2_models import Resource
from scim2_server.mapping import conversion as core
from scim2_server.mapping.descriptors import Collection

from .mapping import ResourceMapping
from .mapping import _as_stored
from .mapping import _Collection
from .mapping import _Column
from .mapping import _related_record

_VERSION_SALT = "scim2_django.version"


def _version_key() -> bytes:
    """Return the key of the hash of the resources without version field.

    The hash is the one salted_hmac gives with the SECRET_KEY setting, so that
    the versions do not change from former releases.
    """
    return hashlib.sha256(
        force_bytes(_VERSION_SALT) + force_bytes(settings.SECRET_KEY)
    ).digest()


def _to_scim(
    mapping: ResourceMapping,
    record: Any,
    resource_type: str,
    model: type[Resource[Any]],
    endpoints: Mapping[str, str],
    collections: Iterable[Collection],
) -> Resource[Any]:
    """Build the resource a record stores, as an instance of model.

    Only the given collections are read: the others may not be loaded.

    The attributes are found by their URN, so model may be another model of
    the same schemas as the model of the mapping, such as a model a provider
    built from the schemas. endpoints gives the endpoint of each resource type
    by name: a link to one of them gets a $ref relative to the SCIM root, when
    model declares it, which the server makes absolute (RFC 7643 §2.3.7).
    """
    return core.to_scim(
        mapping,
        record,
        resource_type,
        model,
        endpoints,
        collections,
        _version_key(),
    )


def _from_scim(
    mapping: ResourceMapping,
    resource: Resource[Any],
    record: Any,
    links: dict[Collection, list[Any]],
    now: Any,
    creating: bool,
) -> None:
    """Write the fields of a resource into a record, before the record is saved.

    Read-only attributes are left as they are. The dates of the record are
    set to now. A Link holding one entry is a foreign key of the record, so it
    is written here too. links holds the records each Link of the resource links
    to.
    """
    for column, value in core.dates(mapping, now, creating):
        if column.relation is None:  # type: ignore[attr-defined]
            setattr(record, column.field_key, value)  # type: ignore[attr-defined]

    for column, value in core.write_plan(mapping, resource, record, creating):
        setattr(record, column.field_key, _stored(column, value))  # type: ignore[attr-defined,arg-type]

    for collection in _written(mapping):
        if collection.single and not collection.owned:
            setattr(record, collection.accessor, next(iter(links[collection]), None))


def _stored(column: _Column, value: Any) -> Any:
    """Return the value a field stores for a SCIM value, an empty string for no value in a text field without null.

    A transformed field stores what to_field gives. The value is left out of
    the error, as it may be a password.
    """
    if column.to_field is not None:
        try:
            stored = column.to_field(value)
        except (TypeError, ValueError, ValidationError):
            raise InvalidValueException(
                detail=f"'{column.binding.urn}' cannot hold this value on this server"
            ) from None

        if stored is None and not column.field.null:  # type: ignore[union-attr]
            raise InvalidValueException(
                detail=f"'{column.binding.urn}' is required on this server"
            )

        return stored

    if value is None:
        return "" if column.empty_string else None

    return _as_stored(value)


def _write_collections(
    mapping: ResourceMapping,
    resource: Resource[Any],
    record: Any,
    links: dict[Collection, list[Any]],
    now: Any,
    creating: bool,
) -> None:
    """Write the entries, the links and the related records of a resource, once its record is saved.

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

    # The handlers of the changes of the links may write the related records
    # too, so they are written last.
    _write_related(mapping, resource, record, now, creating)


def _write_related(
    mapping: ResourceMapping,
    resource: Resource[Any],
    record: Any,
    now: Any,
    creating: bool,
) -> None:
    """Write the fields of a resource that related records hold, once its record is saved.

    A related record is created when it holds a date, or gets a value. Without
    dates, a resource without such values gets no related record.

    A missing value is not written in a new related record, nor when creating
    the resource, so that the field keeps its default, unless to_field gives
    its value for no value. A related record missing when the resource was
    read is new. A new related record needs a value for each mapped field
    without default.
    """
    values: dict[Any, list[tuple[_Column, Any]]] = {}
    for column in _columns(mapping):
        if not column.writable or column.relation is None:
            continue

        value = Path(column.binding.urn).get(resource, strict=False)
        new = creating or _related_record(column, record) is None
        if core.keeps(mapping, column, value, record, new):
            continue

        values.setdefault(column.relation, []).append((column, value))

    for date, value in core.dates(mapping, now, creating):
        if date.relation is not None:  # type: ignore[attr-defined]
            values.setdefault(date.relation, []).append((date, value))  # type: ignore[attr-defined,arg-type]

    for relation, written in values.items():
        # A handler of post_save may have created the related record since
        # it was loaded.
        related = (
            relation.related_model._base_manager.using(record._state.db)
            .filter(**{relation.field.name: record})
            .first()
        )
        if related is None:
            if all(value is None for _, value in written):
                continue

            written = [
                (column, value)
                for column, value in written
                if value is not None or column.transformed
            ]

            _check_creatable(mapping, relation, written)
            related = relation.related_model(**{relation.field.name: record})

        for column, value in written:
            setattr(related, column.field.name, _stored(column, value))  # type: ignore[union-attr]

        related.save(using=record._state.db)


def _check_creatable(
    mapping: ResourceMapping, relation: Any, written: list[tuple[_Column, Any]]
) -> None:
    """Refuse to create a related record without a value for a mapped field that has no default."""
    given = {column for column, _ in written}
    for column in _columns(mapping):
        if column.relation is relation and column not in given:
            if not column.nullable and column.needs_value:
                raise InvalidValueException(
                    detail=f"'{column.binding.urn}' is required on this server"
                )


def _columns(mapping: ResourceMapping) -> Iterable[_Column]:
    """Return the columns of the resource record of a mapping."""
    return mapping._columns.values()  # type: ignore[return-value]


def _written(mapping: ResourceMapping) -> Iterable[_Collection]:
    """Yield the collections that writing a resource changes."""
    return (
        collection  # type: ignore[misc]
        for collection in mapping._all_collections()
        if collection.writable
    )


def _write_entry(collection: _Collection, entry: Any, record: Any) -> models.Model:
    """Build the record storing an entry of a multi-valued attribute, for a resource record."""
    owner = collection.relation.field.name
    entry_record = collection.entry_model(**{owner: record})
    if collection.simple:
        setattr(entry_record, collection.columns[None].key, _as_stored(entry))
        return entry_record

    for column in collection.columns.values():
        value = getattr(entry, column.binding.target_field_name, None)
        if value is not None:
            setattr(entry_record, column.key, _as_stored(value))

    return entry_record
