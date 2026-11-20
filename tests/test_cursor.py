import datetime
import decimal
import uuid

import pytest
from scim2_models import URN
from scim2_models import Extension
from scim2_models import InvalidCursorException
from scim2_models import InvalidPathException
from scim2_models import ResourceType
from scim2_models import SearchRequest
from scim2_models import User

from scim2_django.storage import DjangoStorage
from scim2_django.storage import ResourceMapping
from scim2_django.storage import storage as storage_module
from scim2_django.storage.comparison import _Comparator
from scim2_django.storage.keyset import _decode
from scim2_django.storage.keyset import _encode

from .mappings import users_mapping
from .models import UserRecord
from .test_storage import Server

pytestmark = pytest.mark.django_db

RANKED = "urn:example:params:scim:schemas:extension:ranked:2.0:User"


class Ranked(Extension):
    __schema__ = URN(RANKED)

    rank: int | None = None


@pytest.fixture
def server():
    """Return a server holding five users, two of them without a title."""
    server = Server()
    for user_name, title, emails in [
        ("alice", "b", [{"value": "b@example.com"}, {"value": "a@example.com"}]),
        ("bob", None, []),
        ("carol", "a", [{"value": "b@example.com"}]),
        (
            "dave",
            "b",
            [{"value": "z@example.com"}, {"value": "c@example.com", "primary": True}],
        ),
        ("erin", None, []),
    ]:
        user = server.user_model(user_name=user_name, title=title, emails=emails)
        server.storage.create(server.users, user)

    return server


def walk(storage, resource_types, search_request):
    """Follow the next cursors from the first page, and return the pages.

    A page repeating forever fails the test rather than blocking it.
    """
    pages = [storage.search(resource_types, search_request)]
    while pages[-1].next is not None:
        assert len(pages) < 10
        pages.append(
            storage.search(resource_types, search_request, position=pages[-1].next)
        )

    return pages


def walk_back(storage, resource_types, search_request, page):
    """Follow the previous cursors from a page, and return the pages in the sort order."""
    pages = [page]
    while pages[0].previous is not None:
        pages.insert(
            0,
            storage.search(resource_types, search_request, position=pages[0].previous),
        )

    return pages


def user_names(pages):
    return [resource.user_name for page in pages for resource in page.resources]


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
@pytest.mark.parametrize("sort_by", [None, "title", "emails", "meta.created", "id"])
def test_cursors_page_through_the_sort_and_back(server, sort_by, sort_order):
    """The pages follow the sort, users without a value included, in both directions.

    Two users share a title, and the sort on the emails compares the primary
    email, or else the first one.
    """
    search_request = SearchRequest(
        cursor="", count=2, sort_by=sort_by, sort_order=sort_order
    )
    expected = user_names(
        [
            server.storage.search(
                [server.users], SearchRequest(sort_by=sort_by, sort_order=sort_order)
            )
        ]
    )

    forward = walk(server.storage, [server.users], search_request)
    backward = walk_back(server.storage, [server.users], search_request, forward[-1])

    assert user_names(forward) == expected
    assert user_names(backward) == expected


def test_a_cursor_without_count_returns_every_resource(server):
    """Without count, the first page holds every resource, and no page follows."""
    page = server.storage.search([server.users], SearchRequest(cursor=""))

    assert len(page.resources) == 5
    assert page.next is None
    assert page.previous is None


def test_a_page_reached_forward_assumes_a_previous_page(server):
    """A page after the first one gives a previous cursor, even when the resources before it are gone."""
    search_request = SearchRequest(cursor="", count=2)
    first, second, _ = walk(server.storage, [server.users], search_request)
    for resource in first.resources:
        server.storage.delete(server.users, resource.id)

    previous = server.storage.search(
        [server.users], search_request, position=second.previous
    )

    assert second.previous is not None
    assert previous.resources == []
    assert previous.next is None
    assert previous.previous is None


@pytest.mark.parametrize(
    "position",
    [
        "next",
        {"d": "next", "i": "x"},
        {"d": "sideways", "k": None, "i": "x"},
        {"d": "next", "k": None, "i": 1},
        {"d": "next", "k": {"datetime": "never"}, "i": "x"},
        {"d": "next", "k": None, "i": "not an identifier"},
        {"d": "next", "k": 10**20, "i": str(uuid.uuid4())},
    ],
)
def test_a_malformed_position_is_an_invalid_cursor(server, position):
    """A position the storage did not give raises invalidCursor."""
    with pytest.raises(InvalidCursorException):
        server.storage.search(
            [server.users], SearchRequest(cursor="x"), position=position
        )


@pytest.fixture
def mixed():
    """Return a server holding three users and two groups, and the resource types of both."""
    server = Server()
    for name in ["d", "b", "e"]:
        server.storage.create(
            server.users, server.user_model(user_name=name, display_name=name)
        )

    for name in ["c", "a"]:
        server.storage.create(server.groups, server.group_model(display_name=name))

    server.storage.create(server.groups, server.group_model())
    return server, [server.users, server.groups]


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
@pytest.mark.parametrize("sort_by", [None, "displayName", "userName", "id"])
def test_cursors_page_at_the_root(mixed, sort_by, sort_order):
    """A search at the root pages the resources of every type as one collection, in both directions.

    Groups have no userName, and sort as having no value.
    """
    server, resource_types = mixed
    search_request = SearchRequest(
        cursor="", count=2, sort_by=sort_by, sort_order=sort_order
    )
    expected = [
        resource.id
        for resource in server.storage.search(
            resource_types, SearchRequest(sort_by=sort_by, sort_order=sort_order)
        ).resources
    ]

    forward = walk(server.storage, resource_types, search_request)
    backward = walk_back(server.storage, resource_types, search_request, forward[-1])

    ids = [[r.id for r in page.resources] for page in forward]
    assert [i for page in ids for i in page] == expected
    assert [[r.id for r in page.resources] for page in backward] == ids


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
@pytest.mark.parametrize("sort_by", [None, "displayName", "userName", "id"])
def test_a_search_at_the_root_sorts_as_search_request_sort(mixed, sort_by, sort_order):
    """The resources of every type are sorted together, ties by type then identifier."""
    server, resource_types = mixed
    search_request = SearchRequest(sort_by=sort_by, sort_order=sort_order)
    by_type = [
        sorted(
            server.storage.search([resource_type], SearchRequest()).resources,
            key=lambda r: r.id,
        )
        for resource_type in resource_types
    ]
    resources = [resource for resources in by_type for resource in resources]

    page = server.storage.search(resource_types, search_request)

    assert [r.id for r in page.resources] == [
        r.id for r in search_request.sort(resources)
    ]
    assert page.total == 6


def test_a_search_at_the_root_filters_and_pages_by_index(mixed):
    """The total counts the matches of every type, and the page is cut from startIndex."""
    server, resource_types = mixed
    search_request = SearchRequest(
        filter="displayName pr", sort_by="displayName", start_index=2, count=3
    )

    page = server.storage.search(resource_types, search_request)

    assert page.total == 5
    assert [r.display_name for r in page.resources] == ["b", "c", "d"]


def test_a_resource_deleted_after_its_page_was_selected_is_left_out(mixed, monkeypatch):
    """The rows of the page are selected before their records are loaded."""
    server, resource_types = mixed
    select = storage_module._root_search

    def select_then_delete(*args, **kwargs):
        selected = select(*args, **kwargs)
        UserRecord.objects.filter(user_name="b").delete()
        return selected

    monkeypatch.setattr(storage_module, "_root_search", select_then_delete)

    page = server.storage.search(resource_types, SearchRequest(sort_by="displayName"))

    assert page.total == 6
    assert [r.display_name for r in page.resources] == ["a", "c", "d", "e", None]


@pytest.mark.parametrize(
    "position",
    [
        {"d": "next", "k": None, "i": "x"},
        {"d": "next", "k": None, "t": True, "i": "x"},
        {"d": "next", "k": None, "t": 2, "i": "x"},
        {"d": "next", "k": "alice", "t": 0, "i": "x"},
    ],
)
def test_a_malformed_root_position_is_an_invalid_cursor(mixed, position):
    """A position at the root needs the position of an existing resource type, and a sort value of the sort type."""
    server, resource_types = mixed
    search_request = SearchRequest(cursor="x", sort_by="meta.created")

    with pytest.raises(InvalidCursorException):
        server.storage.search(resource_types, search_request, position=position)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        3,
        1.5,
        "text",
        datetime.datetime(2026, 10, 9, 12, tzinfo=datetime.UTC),
        datetime.date(2026, 10, 9),
        decimal.Decimal("1.50"),
        uuid.UUID(int=1),
    ],
)
def test_a_sort_value_survives_its_position(value):
    """A sort value is read back from its position in its own type."""
    assert _decode(_encode(value)) == value
    assert type(_decode(_encode(value))) is type(value)


def test_a_sort_value_of_an_unknown_type_cannot_be_paged():
    """A sort value JSON cannot hold is refused rather than written wrong."""
    with pytest.raises(TypeError):
        _encode(b"bytes")


@pytest.mark.parametrize(
    "value",
    [["a"], {"a": "1", "b": "2"}, {"bytes": "x"}, {"date": 1}, {"decimal": "x"}],
)
def test_a_malformed_sort_value_is_an_invalid_cursor(value):
    """A sort value that no position holds raises invalidCursor."""
    with pytest.raises(InvalidCursorException):
        _decode(value)


def ranked_mapping(field):
    """Return a mapping of the users with an integer rank, stored in a field."""
    return ResourceMapping(
        User[Ranked],
        UserRecord,
        {
            "id": "id",
            "userName": "user_name",
            f"{RANKED}:rank": field,
            "meta.created": "created",
            "meta.lastModified": "last_modified",
        },
        version="version",
    )


@pytest.fixture
def ranked():
    """Return a storage of a user ranked 1 and a user without rank, with its resource type."""
    storage = DjangoStorage({"User": ranked_mapping("badge_number")})
    users = ResourceType.from_resource(User[Ranked])
    user = User[Ranked](user_name="ranked")
    user[Ranked] = Ranked(rank=1)
    storage.create(users, user)
    storage.create(users, User[Ranked](user_name="unranked"))
    return storage, users


def test_a_sort_value_beyond_its_field_reads_the_values_past_it(ranked):
    """An integer larger than any value of the field is past them all, before the missing ones."""
    storage, users = ranked
    position = {"d": "next", "k": 3_000_000_000, "i": str(uuid.uuid4())}
    search_request = SearchRequest(cursor="x", sort_by=f"{RANKED}:rank")

    page = storage.search([users], search_request, position=position)

    assert [r.user_name for r in page.resources] == ["unranked"]


def test_a_sort_value_of_another_type_is_an_invalid_cursor(ranked):
    """A text where the sort compares integers comes from no position this storage gave."""
    storage, users = ranked
    position = {"d": "next", "k": "alice", "i": str(uuid.uuid4())}
    search_request = SearchRequest(cursor="x", sort_by=f"{RANKED}:rank")

    with pytest.raises(InvalidCursorException):
        storage.search([users], search_request, position=position)


class Generic(DjangoStorage):
    """A storage comparing strings as on a database without comparator of its own."""

    def _comparator(self):
        return _Comparator(self._policy)


def test_cursors_page_on_the_values_the_database_compares():
    """A field converting the values it reads does not shift the pages.

    The codes read back in upper case, while the database sorts them in
    lower case, as externalId is case exact.
    """
    server = Server(Generic, mappings={"User": users_mapping({"externalId": "code"})})
    for code in ("b", "a", "c"):
        server.storage.create(
            server.users, server.user_model(user_name=code, external_id=code)
        )

    pages = walk(
        server.storage,
        [server.users],
        SearchRequest(cursor="", count=1, sort_by="externalId"),
    )

    assert user_names(pages) == ["a", "b", "c"]


def test_a_sort_on_fields_of_different_types_is_refused_at_the_root():
    """The sort values of every resource type fit in one column, of one type."""
    storage = DjangoStorage(
        {"User": ranked_mapping("badge_number"), "Robot": ranked_mapping("version")}
    )
    resource_types = [
        ResourceType.from_resource(User[Ranked]),
        ResourceType(
            id="Robot",
            name="Robot",
            endpoint="/Robots",
            schema_=User.__schema__,
            schema_extensions=ResourceType.from_resource(
                User[Ranked]
            ).schema_extensions,
        ),
    ]

    with pytest.raises(InvalidPathException, match="different types"):
        storage.search(resource_types, SearchRequest(sort_by=f"{RANKED}:rank"))


def test_a_page_at_the_root_loads_its_records_in_batches(mixed, monkeypatch):
    """A page without count loads its records a few at a time, below the parameter limits."""
    server, resource_types = mixed
    monkeypatch.setattr(storage_module, "_LOADED", 2)

    page = server.storage.search(resource_types, SearchRequest(sort_by="displayName"))

    assert [r.display_name for r in page.resources] == ["a", "b", "c", "d", "e", None]
