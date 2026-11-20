import datetime
import decimal
import uuid

import pytest
from scim2_models import URN
from scim2_models import Extension
from scim2_models import InvalidCursorException
from scim2_models import ResourceType
from scim2_models import SearchRequest
from scim2_models import User

from scim2_django.storage import DjangoStorage
from scim2_django.storage import ResourceMapping
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


@pytest.fixture
def ranked():
    """Return a storage of a user ranked 1 and a user without rank, with its resource type."""
    mapping = ResourceMapping(
        User[Ranked],
        UserRecord,
        {
            "id": "id",
            "userName": "user_name",
            f"{RANKED}:rank": "badge_number",
            "meta.created": "created",
            "meta.lastModified": "last_modified",
        },
        version="version",
    )
    storage = DjangoStorage({"User": mapping})
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
