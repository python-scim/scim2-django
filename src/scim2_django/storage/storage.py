import datetime
import threading
from collections.abc import Mapping
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS
from django.db import IntegrityError
from django.db import connections
from django.db import transaction
from django.db.models import F
from scim2_models import InvalidValueException
from scim2_models import NotFoundException
from scim2_models import Path
from scim2_models import PreconditionFailedException
from scim2_models import Resource
from scim2_models import ResourceType
from scim2_models import ResponseParameters
from scim2_models import ScimPolicy
from scim2_models import ScimProvider
from scim2_models import SearchRequest
from scim2_models import UniquenessException
from scim2_models.path import CompareOperator
from scim2_server.keyset import Bound
from scim2_server.keyset import Position
from scim2_server.keyset import keyset_page
from scim2_server.keyset import page_rows
from scim2_server.storage import ScimStorage
from scim2_server.storage import SearchPage

from .comparison import _Comparator
from .comparison import _comparator
from .comparison import _vendor
from .conversion import _as_stored
from .conversion import _check_storable
from .conversion import _comparable
from .conversion import _from_scim
from .conversion import _link_entries
from .conversion import _to_scim
from .conversion import _write_collections
from .mapping import ResourceMapping
from .mapping import _Collection
from .query import _compare
from .query import _exact_key
from .query import _root_search
from .query import _search
from .query import _with_ids

_DUPLICATE_ENTRY = 1062
"""The code of MariaDB for a value a unique index already holds."""


_LOADED = 500
"""The number of records a query loads at most, below the parameter limits of the databases."""


def _by_position(rows: list[dict[str, Any]]) -> dict[int, list[str]]:
    """Group the identifiers of a page at the root by position of their resource type."""
    ids: dict[int, list[str]] = {}
    for row in rows:
        ids.setdefault(row["scim_position"], []).append(row["scim_id"])

    return ids


def _record_bound(mapping: ResourceMapping, record: Any) -> Bound:
    """Return the sort value and the identifier of a record of a page."""
    key = getattr(record, "scim_sort_key", None)
    return Bound(key, str(getattr(record, mapping._id.key)))


def _row_bound(row: dict[str, Any]) -> Bound:
    """Return the sort value, the identifier and the resource type position of a row of a page at the root."""
    return Bound(row.get("scim_sort_key"), row["scim_id"], row["scim_position"])


def _now() -> Any:
    """Return the current time, in the form the date fields hold it."""
    return _as_stored(datetime.datetime.now(datetime.UTC))


class DjangoStorage(ScimStorage):
    """Store SCIM resources in the records of Django models.

    Each write runs in :func:`~django.db.transaction.atomic`, and so does each
    SCIM operation, through :meth:`operation`. The storage never commits
    itself: outside of a transaction, Django commits each atomic block.

    A change the mappings cannot store raises an error rather than being
    lost: :class:`~scim2_models.InvalidValueException` for an attribute no
    mapping stores, and :class:`~scim2_models.MutabilityException` for an
    attribute the mapping only reads. A value equal to the stored one is
    accepted, so a resource read and sent back can always be written. The
    ``primary`` of the lone entry of an :class:`Entry` is the exception: it
    accepts any value, and reads back as the primary one.

    The storage searches one resource type, or several at the root of the
    server as one collection. It pages by index, or with cursors that give
    stable pages (:rfc:`RFC 9865 <9865>`).

    The storage runs on SQLite, PostgreSQL and MariaDB 10.11 or later.
    Filters and sorts compare strings in the database, whatever the
    collation of the columns. SQLite compares them with the comparison key of
    the policy. PostgreSQL and MariaDB approach the default comparison key:
    they lower the strings unless the attribute is case exact, and order them
    by code point. PostgreSQL also normalizes them to NFC, which MariaDB
    cannot do. Other databases compare strings with their own ``lower()`` and
    collation, with a warning.

    On SQLite, set ``"transaction_mode": "IMMEDIATE"`` in the
    :setting:`OPTIONS <DATABASE-OPTIONS>` of the database, so that a write
    waits for another one rather than failing with ``database is locked``.
    On MariaDB, the unique index of a field follows the collation of its
    column: a collation ignoring the accents refuses two values that the
    storage tells apart, so the write raises
    :class:`~scim2_models.UniquenessException`. With Django 5.2, set
    ``"charset": "utf8mb4"`` in the :setting:`OPTIONS <DATABASE-OPTIONS>` of
    the database, so that the strings keep their characters of four bytes.

    :param mappings: The mapping of each resource type, by resource type name.
    :param provider: The provider of the server. The storage then returns
        instances of its models, rather than of the models of the mappings,
        and gives each link a ``$ref`` relative to the SCIM root, from the
        endpoints of its resource types. Strings are compared with the
        :attr:`~scim2_models.ScimPolicy.comparison_key` of its policy.
    :param using: The alias of the database holding the records.
    """

    supports_cursors = True

    def __init__(
        self,
        mappings: Mapping[str, ResourceMapping],
        provider: ScimProvider | None = None,
        *,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        self.mappings = dict(mappings)
        self.provider = provider
        self.using = using
        self._policy = provider.policy if provider else ScimPolicy()
        self._endpoints = {
            str(resource_type.name): str(resource_type.endpoint).strip("/")
            for resource_type in (provider.resource_types if provider else ())
        }
        self._comparators: dict[str, _Comparator] = {}
        self._bound = False
        self._lock = threading.Lock()

    def operation(self) -> transaction.Atomic:
        """Run the operation in :func:`~django.db.transaction.atomic`.

        A failed operation of a bulk request is then rolled back alone
        (:rfc:`RFC 7644 §3.7 <7644#section-3.7>`).
        """
        return transaction.atomic(using=self.using)

    def get(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        response_parameters: ResponseParameters[Any] | None = None,
    ) -> Resource[Any]:
        """Return a resource, with the attributes the response keeps."""
        record = self._load(resource_type, resource_id, response_parameters)
        return self._to_scim(resource_type, record, response_parameters)

    def search(
        self,
        resource_types: list[ResourceType],
        search_request: SearchRequest[Any],
        *,
        position: Any = None,
    ) -> SearchPage:
        """Return a page of the resources matching a search, on one resource type or at the root."""
        if not resource_types:
            return SearchPage(0, [])

        if len(resource_types) > 1:
            return self._root_search(resource_types, search_request, position)

        (resource_type,) = resource_types
        mapping = self._mapping(resource_type)
        anchor = Position.load(position, has_sort=search_request.sort_by is not None)
        matching, records = _search(
            mapping, search_request, self._comparator(), self.using, anchor
        )
        page, more = page_rows(list(records), search_request, anchor)
        resources = [
            self._to_scim(resource_type, record, search_request) for record in page
        ]
        total = matching.count()
        if search_request.cursor is None:
            return SearchPage(total, resources)

        first = _record_bound(mapping, page[0]) if page else None
        last = _record_bound(mapping, page[-1]) if page else None
        return keyset_page(total, resources, first, last, more, anchor)

    def _root_search(
        self,
        resource_types: list[ResourceType],
        search_request: SearchRequest[Any],
        position: Any,
    ) -> SearchPage:
        """Return a page of the resources of several resource types, sorted and paged as one collection.

        A record deleted after the page was selected is left out.
        """
        mappings = [self._mapping(resource_type) for resource_type in resource_types]
        anchor = Position.load(
            position,
            has_sort=search_request.sort_by is not None,
            type_count=len(mappings),
        )
        total, rows = _root_search(
            mappings, search_request, self._comparator(), self.using, anchor
        )
        rows, more = page_rows(rows, search_request, anchor)
        records: dict[tuple[int, str], Any] = {}
        for type_position, ids in _by_position(rows).items():
            mapping = mappings[type_position]
            collections = mapping._returned_collections(search_request)
            for start in range(0, len(ids), _LOADED):
                loaded = mapping._queryset(collections, self.using).filter(
                    _with_ids(
                        mapping._id, ids[start : start + _LOADED], self._comparator()
                    )
                )
                for record in loaded:
                    record_id = str(getattr(record, mapping._id.key))
                    records[type_position, record_id] = record

        found = [
            row for row in rows if (row["scim_position"], row["scim_id"]) in records
        ]
        resources = [
            self._to_scim(
                resource_types[row["scim_position"]],
                records[row["scim_position"], row["scim_id"]],
                search_request,
            )
            for row in found
        ]
        if search_request.cursor is None:
            return SearchPage(total, resources)

        first = _row_bound(rows[0]) if rows else None
        last = _row_bound(rows[-1]) if rows else None
        return keyset_page(total, resources, first, last, more, anchor)

    def create(
        self, resource_type: ResourceType, resource: Resource[Any]
    ) -> Resource[Any]:
        """Store a new resource, and return it as stored."""
        mapping = self._mapping(resource_type)
        with transaction.atomic(using=self.using):
            _check_storable(mapping, resource, None)
            self._check_uniqueness(mapping, resource, None)
            links = self._links(mapping, resource)
            record = mapping.record()
            now = _now()
            _from_scim(mapping, resource, record, links, now, creating=True)
            self._save(mapping, resource, record, links, now, None)

        records = mapping._queryset(mapping._returned_collections(), self.using)
        return self._to_scim(resource_type, records.get(pk=record.pk))

    def update(
        self,
        resource_type: ResourceType,
        resource: Resource[Any],
        *,
        expected_version: str | None = None,
    ) -> Resource[Any]:
        """Replace a resource, and return it as stored.

        The record is locked until the end of the transaction, on the
        databases that lock rows. With an ``expected_version`` the stored
        version does not match, the update raises
        :class:`~scim2_models.PreconditionFailedException`. With a version
        field, so does a write by another transaction since the record was
        read, even without ``expected_version``.
        """
        mapping = self._mapping(resource_type)
        with transaction.atomic(using=self.using):
            record = self._load(resource_type, resource.id, locked=True)
            stored = self._to_scim(resource_type, record)
            self._check_version(stored, expected_version)
            _check_storable(mapping, resource, stored)
            self._check_uniqueness(mapping, resource, stored)
            links = self._links(mapping, resource)
            self._count_write(mapping, record)
            now = _now()
            _from_scim(mapping, resource, record, links, now, creating=False)
            self._save(mapping, resource, record, links, now, stored)

        return self.get(resource_type, resource.id)  # type: ignore[arg-type]

    def delete(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected_version: str | None = None,
    ) -> None:
        """Delete a resource."""
        mapping = self._mapping(resource_type)
        with transaction.atomic(using=self.using):
            record = self._load(resource_type, resource_id, locked=True)
            if expected_version is not None:
                stored = self._to_scim(resource_type, record)
                self._check_version(stored, expected_version)

            self._count_write(mapping, record)
            record.delete(using=self.using)

    def _mapping(self, resource_type: ResourceType) -> ResourceMapping:
        """Return the mapping of a resource type, once the Links of every mapping are bound.

        The mappings are checked and bound at the first operation, rather than
        when the storage is created, as the URLconf creates it before the
        system checks run.
        """
        with self._lock:
            if not self._bound:
                self._bind()

        mapping = self.mappings.get(resource_type.name)  # type: ignore[arg-type]
        if mapping is None:
            raise ImproperlyConfigured(
                f"No mapping for the resource type {resource_type.name!r}"
            )

        return mapping

    def _bind(self) -> None:
        """Bind the Links of every mapping, or none of them when one is refused.

        A resource type of the provider without mapping is refused too.
        """
        resource_types = self.provider.resource_types if self.provider else []
        for resource_type in resource_types:
            if resource_type.name not in self.mappings:
                raise ImproperlyConfigured(
                    f"No mapping for the resource type {resource_type.name!r}"
                )

        links = [mapping._targets(self.mappings) for mapping in self.mappings.values()]
        for mapping, targets in zip(self.mappings.values(), links, strict=True):
            mapping._bind(targets)

        self._bound = True

    def _comparator(self) -> _Comparator:
        """Return how the database compares strings, ready to compare them on the current connection."""
        vendor = _vendor(connections[self.using])
        if vendor not in self._comparators:
            self._comparators[vendor] = _comparator(
                vendor, self._policy, self.mappings.values()
            )

        comparator = self._comparators[vendor]
        comparator.prepare(self.using)
        return comparator

    def _model(self, resource_type: ResourceType) -> type[Resource[Any]]:
        """Return the model of the resources of a resource type: the one of the provider, if any."""
        model = self.provider.model_for(resource_type) if self.provider else None
        return model or self._mapping(resource_type).model  # type: ignore[return-value]

    def _to_scim(
        self,
        resource_type: ResourceType,
        record: Any,
        parameters: ResponseParameters[Any] | None = None,
    ) -> Resource[Any]:
        """Build the resource a record stores, with the collections the response keeps."""
        mapping = self._mapping(resource_type)
        return _to_scim(
            mapping,
            record,
            resource_type.name,  # type: ignore[arg-type]
            self._model(resource_type),
            self._endpoints,
            mapping._returned_collections(parameters),
        )

    def _load(
        self,
        resource_type: ResourceType,
        resource_id: str | None,
        parameters: ResponseParameters[Any] | None = None,
        locked: bool = False,
    ) -> Any:
        """Load a record, with the collections the response keeps, all of them without parameters.

        A locked record stays locked until the end of the transaction.
        """
        mapping = self._mapping(resource_type)
        collections = mapping._returned_collections(parameters)
        records = mapping._queryset(collections, self.using)
        relations = mapping._relations()
        if locked and self._locks_tables():
            # FOR UPDATE fails on the nullable side of an outer join, which
            # select_related and the expressions reading related records add,
            # so the related records are locked apart.
            parents = mapping.record._meta.parents.values()
            records = records.select_for_update(
                of=("self", *(link.name for link in parents if link is not None))
            )
        elif locked:
            records = records.select_for_update()

        try:
            record = records.get(
                _with_ids(mapping._id, [resource_id], self._comparator())
            )
        except mapping.record.DoesNotExist:
            raise self._not_found(resource_type, resource_id) from None

        if locked:
            self._lock_related(relations, record)
            self._read_expressions(mapping, record)

        return record

    def _locks_tables(self) -> bool:
        """Tell whether the database locks the rows of chosen tables only."""
        return bool(connections[self.using].features.has_select_for_update_of)

    def _lock_related(self, relations: list[Any], record: Any) -> None:
        """Read again, and lock, the related records holding fields of a locked record.

        Another transaction may have written them while this one waited for
        the lock, or created them.
        """
        for relation in relations:
            related = (
                relation.related_model._default_manager.using(self.using)
                .select_for_update()
                .filter(**{relation.field.name: record})
            )
            relation.set_cached_value(record, next(iter(related), None))

    def _read_expressions(self, mapping: ResourceMapping, record: Any) -> None:
        """Compute again the expressions of a locked record.

        They may read related records, which another transaction may have
        written while this one waited for the lock.
        """
        annotations = mapping._annotations()
        if not annotations:
            return

        values = (
            mapping.record._default_manager.using(self.using)
            .filter(pk=record.pk)
            .values(**annotations)
            .get()
        )
        for alias, value in values.items():
            setattr(record, alias, value)

    @staticmethod
    def _not_found(
        resource_type: ResourceType, resource_id: str | None
    ) -> NotFoundException:
        """Return the error of a resource that does not exist."""
        return NotFoundException(
            detail=f"{resource_type.name} {resource_id!r} not found"
        )

    @staticmethod
    def _check_version(stored: Resource[Any], expected_version: str | None) -> None:
        """Refuse to change a resource whose version is not the expected one."""
        if expected_version is None:
            return

        if stored.meta.version != expected_version:  # type: ignore[union-attr]
            raise PreconditionFailedException

    def _count_write(self, mapping: ResourceMapping, record: Any) -> None:
        """Count a write in the version of a record.

        The version is raised only if it is still the one the record was read
        with, so that a write by another transaction since raises a 412 rather
        than being lost. Databases that lock rows wait for the other transaction instead.
        Without a version field, the hash of the resource changes by itself.
        """
        if mapping.version is None:
            return

        version = getattr(record, mapping.version)
        written = (
            mapping.record._default_manager.using(self.using)
            .filter(pk=record.pk, **{mapping.version: version})
            .update(**{mapping.version: F(mapping.version) + 1})
        )
        if not written:
            raise PreconditionFailedException

        setattr(record, mapping.version, version + 1)

    def _save(
        self,
        mapping: ResourceMapping,
        resource: Resource[Any],
        record: Any,
        links: dict[_Collection, list[Any]],
        now: Any,
        stored: Resource[Any] | None,
    ) -> None:
        """Save a record and its collections, at the date now. stored is the stored resource, None for a new one.

        On an integrity error, the uniqueness is checked again, so that a
        unique value taken in the meantime raises a 409. So does a value a
        unique index of the database refuses, although the storage would
        accept it, such as a value differing from another by its accents
        under a collation ignoring them.
        """
        try:
            with transaction.atomic(using=self.using):
                record.save(using=self.using)
                _write_collections(
                    mapping, resource, record, links, now, creating=stored is None
                )
        except IntegrityError as error:
            self._check_uniqueness(mapping, resource, stored)
            if _is_duplicate(error):
                raise UniquenessException from error

            raise

    def _check_uniqueness(
        self,
        mapping: ResourceMapping,
        resource: Resource[Any],
        stored: Resource[Any] | None,
    ) -> None:
        """Refuse a resource with a unique value another resource already has.

        stored is the stored resource, None for a new one. A value it already
        holds is not checked, so that values another application wrote can
        stay as they are. A missing value never clashes, as a SQL NULL does
        not.
        """
        comparator = self._comparator()
        others = mapping.record._default_manager.using(self.using)
        if stored is not None:
            others = others.exclude(_with_ids(mapping._id, [stored.id], comparator))

        for column in mapping._unique_columns():
            value = Path(column.binding.urn).get(resource, strict=False)
            current = (
                None
                if stored is None
                else Path(column.binding.urn).get(stored, strict=False)
            )
            if value is None or _comparable(value) == _comparable(current):
                continue

            condition = _compare(comparator, column, CompareOperator.eq, value)
            if others.filter(condition).exists():
                raise UniquenessException

    def _links(
        self, mapping: ResourceMapping, resource: Resource[Any]
    ) -> dict[_Collection, list[Any]]:
        """Return the records each Link of a resource links to, refusing a value that links to nothing."""
        links: dict[_Collection, list[Any]] = {}
        for collections, entries in _link_entries(mapping, resource):
            ids = [entry.value for entry in entries]
            found = [
                (collection, self._linked_records(collection, ids))
                for collection in collections
            ]
            links |= self._assign_links(found, entries)

        for collection in mapping._all_collections():
            if not collection.owned:
                links.setdefault(collection, [])

        return links

    def _linked_records(self, collection: _Collection, ids: list[str]) -> list[Any]:
        """Return the records of a Link holding one of the identifiers."""
        target = collection.target
        assert target is not None
        records = target.record._default_manager.using(self.using).filter(
            _with_ids(target._id, ids, self._comparator())
        )
        if target._id.transformed:
            records = records.annotate(**{target._id.key: target._id.expression})

        return list(records)

    def _assign_links(
        self, found: list[tuple[_Collection, list[Any]]], entries: list[Any]
    ) -> dict[_Collection, list[Any]]:
        """Give each entry of an attribute the record it links to, in the collection of the first Link holding that record.

        found holds the records each Link of the attribute holds among the
        values of the entries. An entry with a type links to a Link giving
        the same type.
        """
        links: dict[_Collection, list[Any]] = {
            collection: [] for collection, _ in found
        }
        urn = found[0][0].binding.urn
        for entry in entries:
            matches = [
                (collection, record)
                for collection, records in found
                for record in records
                if _holds_id(collection, record, entry.value)
            ]
            if not matches:
                raise InvalidValueException(
                    detail=f"'{urn}' links to an unknown resource"
                )

            entry_type = getattr(entry, "type", None)
            if entry_type is not None:
                matches = [
                    (collection, record)
                    for collection, record in matches
                    if self._same_type(collection, entry_type)
                ]

            if not matches:
                raise InvalidValueException(
                    detail=f"'{urn}' links to {entry.value!r}, which is not "
                    f"of type {entry_type!r}"
                )

            collection, record = matches[0]
            if record not in links[collection]:
                links[collection].append(record)

        return links

    def _same_type(self, collection: _Collection, value: str) -> bool:
        """Tell whether a type is the one a Link gives, compared as filters compare it."""
        binding = collection.type_column.binding  # type: ignore[union-attr]
        try:
            return bool(
                binding.comparable(value, self._policy)
                == binding.comparable(collection.link_type, self._policy)
            )
        except ValueError:
            return False


def _is_duplicate(error: IntegrityError) -> bool:
    """Tell whether an integrity error comes from a unique index, by the code the driver of each database gives."""
    cause: Any = error.__cause__
    return bool(
        getattr(cause, "sqlstate", None) == "23505"
        or getattr(cause, "sqlite_errorname", None)
        in ("SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY")
        or cause.args[:1] == (_DUPLICATE_ENTRY,)
    )


def _holds_id(collection: _Collection, record: Any, value: str) -> bool:
    """Tell whether a record a Link links to has an identifier.

    A record holds the expression of a transformed identifier, which gives
    the identifier clients read.
    """
    target_id = collection.target._id  # type: ignore[union-attr]
    if target_id.transformed:
        return bool(getattr(record, target_id.key) == value)

    return bool(getattr(record, target_id.key) == _exact_key(target_id, value))
