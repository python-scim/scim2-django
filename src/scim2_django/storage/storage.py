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
from scim2_models import NotImplementedException
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
from scim2_server.storage import ScimStorage
from scim2_server.storage import SearchPage

from .comparison import _Comparator
from .comparison import _comparator
from .conversion import _as_stored
from .conversion import _check_storable
from .conversion import _from_scim
from .conversion import _link_entries
from .conversion import _to_scim
from .conversion import _version
from .conversion import _write_collections
from .keyset import _Anchor
from .keyset import _anchor
from .keyset import _position
from .mapping import ResourceMapping
from .mapping import _Collection
from .query import _as_key
from .query import _compare
from .query import _search


def _page_rows(
    rows: list[Any], search_request: SearchRequest[Any], anchor: _Anchor | None
) -> tuple[list[Any], bool]:
    """Return the rows of a page in the sort order, and whether more rows follow in the reading direction.

    The rows of a cursor page were read count + 1 at a time, in the reverse
    order for a previous page.
    """
    if search_request.cursor is None:
        return rows, False

    count = search_request.count
    more = count is not None and len(rows) > count
    page = rows if count is None else rows[:count]
    if anchor is not None and not anchor.forward:
        page.reverse()

    return page, more


def _cursor_page(
    total: int,
    resources: list[Resource[Any]],
    bounds: list[tuple[Any, tuple[Any, ...]]],
    more: bool,
    anchor: _Anchor | None,
) -> SearchPage:
    """Return a cursor page with the positions of the pages around it.

    bounds holds the sort value and the ties of the first and the last rows
    of the page. A page is known to follow in the reading direction only. In
    the other direction, the page the client comes from is assumed to still
    exist.
    """
    page = SearchPage(total, resources)
    if not bounds:
        return page

    forward = anchor is None or anchor.forward
    if more or not forward:
        page.next = _position("next", *bounds[-1])

    if anchor is not None and (more or forward):
        page.previous = _position("previous", *bounds[0])

    return page


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
    accepted, so a resource read and sent back can always be written.

    The storage searches one resource type at a time. It pages by index, or
    with cursors that give stable pages (:rfc:`RFC 9865 <9865>`).

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
        """Return a page of the resources of one resource type matching a search.

        :raises ~scim2_models.NotImplementedException: When the search is on
            several resource types, at the root of the server.
        """
        if not resource_types:
            return SearchPage(0, [])

        if len(resource_types) > 1:
            raise NotImplementedException(
                detail="This server does not search several resource types at once"
            )

        (resource_type,) = resource_types
        mapping = self._mapping(resource_type)
        anchor = _anchor(position)
        matching, records = _search(
            mapping, search_request, self._comparator(), self.using, anchor
        )
        page, more = _page_rows(list(records), search_request, anchor)
        resources = [
            self._to_scim(resource_type, record, search_request) for record in page
        ]
        total = matching.count()
        if search_request.cursor is None:
            return SearchPage(total, resources)

        bounds = [
            (
                getattr(record, "scim_sort_key", None),
                (str(getattr(record, mapping._id.key)),),
            )
            for record in page[:1] + page[-1:]
        ]
        return _cursor_page(total, resources, bounds, more, anchor)

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
            setattr(record, mapping._created.key, now)
            setattr(record, mapping._last_modified.key, now)
            _from_scim(mapping, resource, record, links, creating=True)
            self._save(mapping, resource, record, links, None)

        return self.get(resource_type, str(getattr(record, mapping._id.key)))

    def update(
        self,
        resource_type: ResourceType,
        resource: Resource[Any],
        *,
        expected_version: str | None = None,
    ) -> Resource[Any]:
        """Replace a resource, and return it as stored.

        The record is locked until the end of the transaction, on the
        databases that lock rows. A write by another transaction in the
        meantime raises :class:`~scim2_models.PreconditionFailedException`.
        """
        mapping = self._mapping(resource_type)
        with transaction.atomic(using=self.using):
            record = self._load(resource_type, resource.id, locked=True)
            self._check_version(mapping, record, expected_version)
            stored = self._to_scim(resource_type, record)
            _check_storable(mapping, resource, stored)
            self._check_uniqueness(mapping, resource, stored)
            links = self._links(mapping, resource)
            self._count_write(mapping, record)
            _from_scim(mapping, resource, record, links, creating=False)
            setattr(record, mapping._last_modified.key, _now())
            self._save(mapping, resource, record, links, stored)

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
            self._check_version(mapping, record, expected_version)
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
        vendor = connections[self.using].vendor
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
        key = _as_key(mapping._id, resource_id)
        if key is None:
            raise self._not_found(resource_type, resource_id)

        collections = mapping._returned_collections(parameters)
        records = mapping._queryset(collections, self.using)
        if locked:
            records = records.select_for_update()

        try:
            return records.get(**{mapping._id.key: key})
        except mapping.record.DoesNotExist:
            raise self._not_found(resource_type, resource_id) from None

    @staticmethod
    def _not_found(
        resource_type: ResourceType, resource_id: str | None
    ) -> NotFoundException:
        """Return the error of a resource that does not exist."""
        return NotFoundException(
            detail=f"{resource_type.name} {resource_id!r} not found"
        )

    @staticmethod
    def _check_version(
        mapping: ResourceMapping, record: Any, expected_version: str | None
    ) -> None:
        """Refuse to change a record whose version is not the expected one."""
        if expected_version is None:
            return

        if _version(mapping, record) != expected_version:
            raise PreconditionFailedException

    def _count_write(self, mapping: ResourceMapping, record: Any) -> None:
        """Count a write in the version of a record.

        The version is raised only if it is still the one the record was read
        with, so that a write by another transaction since raises a 412 rather
        than being lost. Databases that lock rows wait for the other transaction instead.
        """
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
        stored: Resource[Any] | None,
    ) -> None:
        """Save a record and its collections. stored is the stored resource, None for a new one.

        On an integrity error, the uniqueness is checked again, so that a
        unique value taken in the meantime raises a 409.
        """
        try:
            with transaction.atomic(using=self.using):
                record.save(using=self.using)
                _write_collections(mapping, resource, record, links)
        except IntegrityError:
            self._check_uniqueness(mapping, resource, stored)
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
            others = others.exclude(
                **{mapping._id.key: _as_key(mapping._id, stored.id)}
            )

        for column in mapping._unique_columns():
            value = Path(column.binding.urn).get(resource, strict=False)
            if value is None:
                continue

            current = (
                None
                if stored is None
                else Path(column.binding.urn).get(stored, strict=False)
            )
            if value == current:
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
        keys = [key for key in (_as_key(target._id, i) for i in ids) if key is not None]
        records = target.record._default_manager.using(self.using)
        return list(records.filter(**{f"{target._id.key}__in": keys}))

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


def _holds_id(collection: _Collection, record: Any, value: str) -> bool:
    """Tell whether a record a Link links to has an identifier."""
    target_id = collection.target._id  # type: ignore[union-attr]
    return bool(getattr(record, target_id.key) == _as_key(target_id, value))
