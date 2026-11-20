import datetime
import warnings

import pytest
from django.db import connection
from django.db.models import BooleanField
from django.db.models import ExpressionWrapper
from django.db.models import Q
from django.db.models import TextField
from django.db.models import Value
from django.db.models.functions import Concat
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import ScimFilter
from scim2_models import ScimPolicy
from scim2_models import SearchRequest
from scim2_server.testing import SEARCH_FILTERS
from scim2_server.testing import SEARCH_SORTS
from scim2_server.testing import SEARCH_USERS

from scim2_django.storage import Computed
from scim2_django.storage import Many
from scim2_django.storage.comparison import _comparator
from scim2_django.storage.conversion import _to_scim
from scim2_django.storage.query import _search

from .mappings import ENTERPRISE
from .mappings import groups_mapping
from .mappings import users_mapping
from .models import GroupRecord
from .models import PhoneRecord
from .models import TaggedUser
from .models import UserRecord
from .test_conversion import bound
from .test_conversion import collections
from .test_conversion import save
from .test_conversion import to_scim
from .test_mapping import TAGGED
from .test_mapping import tagged_mapping

pytestmark = pytest.mark.django_db

NOW = datetime.datetime(2026, 11, 20, 8, 0, tzinfo=datetime.UTC)

LINK_FILTERS = [
    "groups pr",
    'groups.display eq "admins"',
    'groups.type eq "direct"',
    'groups[display sw "st"]',
    f"{ENTERPRISE}:manager pr",
    f'{ENTERPRISE}:manager.displayName eq "ALICE"',
    'locale eq "bob"',
    'locale co "AR"',
    'nonexistent eq "x"',
    "nonexistent pr",
    'nonexistent[value eq "x"]',
    'preferredLanguage lt "m"',
    'preferredLanguage eq ""',
    "preferredLanguage pr",
    'not (preferredLanguage eq "fr")',
]
"""Filters on links, and on a generated field."""

GROUP_FILTERS = [
    "members pr",
    'members.type eq "Group"',
    'members[type eq "User" and display eq "bob"]',
    'members.display co "a"',
    'members ne "x"',
    'displayName lt "B"',
    "externalId pr",
    'externalId eq "1"',
]


class Search:
    """The users of SEARCH_USERS, with groups and a manager, and the resources they read as.

    vendor tells which database the strings are compared for, the database
    of the tests by default. Databases without a comparator of their own
    compare them with lower().
    """

    def __init__(self, users=None, vendor=None):
        self.users, self.groups = bound(users)
        alice, bob, carol, dave = (
            self.store(self.users, UserRecord(), data) for data in SEARCH_USERS
        )
        manager = next(c for c in collections(self.users) if c.single)
        save(
            self.users,
            self.users.model.model_validate(SEARCH_USERS[1]),
            bob,
            {manager: [alice]},
            creating=False,
        )
        staff = GroupRecord.objects.create(
            display_name="Staff", created=NOW, last_modified=NOW
        )
        staff.members.add(bob)
        admins = GroupRecord.objects.create(
            display_name="Admins", external_id="1", created=NOW, last_modified=NOW
        )
        admins.members.add(alice, carol)
        admins.subgroups.add(staff)
        GroupRecord.objects.create(display_name="Empty", created=NOW, last_modified=NOW)
        alice.preferred_language = "fr"
        alice.save()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            self.comparator = _comparator(
                vendor or connection.vendor, ScimPolicy(), [self.users, self.groups]
            )

        self.comparator.prepare("default")

    def store(self, mapping, record, data):
        return save(mapping, mapping.model.model_validate(data), record)

    def mapping(self, record):
        return self.users if record is UserRecord else self.groups

    def resources(self, record):
        mapping = self.mapping(record)
        resource_type = "User" if record is UserRecord else "Group"
        return [
            to_scim(mapping, instance, resource_type)
            for instance in mapping._queryset(collections(mapping)).order_by("pk")
        ]

    def search(self, record, **parameters):
        """Return the total of a search, and the identifiers of its page."""
        mapping = self.mapping(record)
        matching, page = _search(mapping, SearchRequest(**parameters), self.comparator)
        return matching.count(), [str(instance.pk) for instance in page]

    def matching(self, record, scim_filter):
        """Return the identifiers of the resources ScimFilter.match keeps."""
        evaluator = ScimFilter[self.mapping(record).model](scim_filter)
        return [r.id for r in self.resources(record) if evaluator.match(r)]


@pytest.fixture(params=["database", "other"])
def stored(request):
    """Return the stored users, compared as the database of the tests does, or with lower()."""
    return Search(vendor=None if request.param == "database" else request.param)


@pytest.mark.parametrize("scim_filter", SEARCH_FILTERS + LINK_FILTERS)
def test_a_filter_keeps_the_users_kept_in_memory(stored, scim_filter):
    """The database keeps the users that ScimFilter.match keeps, and counts them."""
    expected = stored.matching(UserRecord, scim_filter)

    total, ids = stored.search(UserRecord, filter=scim_filter)

    assert sorted(ids) == sorted(expected)
    assert total == len(expected)


@pytest.mark.parametrize("scim_filter", GROUP_FILTERS)
def test_a_filter_keeps_the_groups_kept_in_memory(stored, scim_filter):
    """Links to several resource types are filtered on each of them."""
    expected = stored.matching(GroupRecord, scim_filter)

    _, ids = stored.search(GroupRecord, filter=scim_filter)

    assert sorted(ids) == sorted(expected)


def test_an_identifier_is_compared_in_its_own_type(stored):
    """A UUID is found by its text, and a text that is not one finds nothing."""
    alice = UserRecord.objects.get(user_name="alice")

    assert stored.search(UserRecord, filter=f'id eq "{alice.pk}"')[1] == [str(alice.pk)]
    assert stored.search(UserRecord, filter=f'id eq "{str(alice.pk).upper()}"')[0] == 0
    assert stored.search(UserRecord, filter='id eq "nope"')[0] == 0
    assert stored.search(UserRecord, filter='id ne "nope"')[0] == 4
    assert stored.search(UserRecord, filter=f'id ne "{alice.pk}"')[0] == 3


def test_a_member_is_found_by_its_identifier(stored):
    """The value of a link is the identifier of the linked resource."""
    bob = UserRecord.objects.get(user_name="Bob")
    staff = GroupRecord.objects.get(display_name="Staff")

    _, ids = stored.search(GroupRecord, filter=f'members.value eq "{bob.pk}"')

    assert ids == [str(staff.pk)]


def strict_key(binding, value):
    """Fold the case fully, and refuse the strings holding an exclamation mark."""
    if "!" in value:
        raise ValueError("'!' cannot be compared")

    return value if binding.case_exact else value.casefold()


def test_sqlite_compares_with_the_key_of_the_policy():
    """A string the key refuses is equal to no other value, stored or searched."""
    if connection.vendor != "sqlite":
        pytest.skip("Only SQLite runs the key of the policy")

    search = Search()
    search.store(search.users, UserRecord(), {"userName": "eve!"})
    search.comparator = _comparator(
        "sqlite", ScimPolicy(comparison_key=strict_key), [search.users]
    )
    search.comparator.prepare("default")

    assert search.search(UserRecord, filter='userName eq "BOB"')[0] == 1
    assert search.search(UserRecord, filter='userName eq "a!"')[0] == 0
    assert search.search(UserRecord, filter='userName ne "a!"')[0] == 5
    assert search.search(UserRecord, filter='userName co "e"')[0] == 2


def test_a_decomposed_value_matches_its_composed_form():
    """Both sides are compared in NFC, whatever form the value was written in."""
    search = Search()
    search.store(search.users, UserRecord(), {"userName": "Jose\u0301"})

    assert search.search(UserRecord, filter='userName eq "JOSÉ"')[0] == 1
    assert search.search(UserRecord, filter='userName ew "sé"')[0] == 1


@pytest.mark.parametrize(
    ("scim_filter", "total"),
    [
        ('externalId sw "AB"', 0),
        ('externalId co "B"', 0),
        ('externalId ew "BC"', 0),
        ('externalId sw "ab"', 1),
        ('externalId co "b"', 1),
        ('externalId ew "bc"', 1),
        ('externalId gt "B"', 1),
    ],
)
def test_a_case_exact_attribute_keeps_its_case(scim_filter, total):
    """Substrings and orderings compare the case of a case exact attribute."""
    search = Search()
    search.store(search.users, UserRecord(), {"userName": "eve", "externalId": "abc"})

    assert search.search(UserRecord, filter=scim_filter)[0] == total


def test_postgresql_warns_it_cannot_follow_the_key_of_the_policy():
    """PostgreSQL follows the default comparison key only."""
    with pytest.warns(UserWarning, match="cannot compare strings"):
        _comparator("postgresql", ScimPolicy(comparison_key=strict_key), [])

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _comparator("postgresql", ScimPolicy(), [])


def test_another_database_warns_it_compares_with_lower():
    """Without a comparator of its own, a database may ignore the case of case exact attributes."""
    with pytest.warns(UserWarning, match=r"lower\(\)"):
        _comparator("mysql", ScimPolicy(), [])


def test_a_comparator_finds_the_links_bound_after_it():
    """The key of a link display is found, even when the comparator was built first."""
    users, groups = users_mapping(), groups_mapping()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        comparator = _comparator(connection.vendor, ScimPolicy(), [users, groups])

    search = Search(users)
    search.comparator = comparator
    comparator.prepare("default")

    assert search.search(UserRecord, filter='groups.display eq "STAFF"')[0] == 1


@pytest.mark.parametrize(
    ("scim_filter", "total"),
    [
        ('id sw "{prefix}"', 1),
        ('id co "{prefix}"', 1),
        ('id ew "{suffix}"', 1),
        ('id le "{id}"', 1),
        ('id gt "{id}"', 0),
    ],
)
def test_a_uuid_is_compared_as_the_text_clients_read(scim_filter, total):
    """The dashes of a UUID are compared, whether the database stores them or not."""
    search = Search()
    UserRecord.objects.exclude(user_name="alice").delete()
    alice = str(UserRecord.objects.get().pk)
    scim_filter = scim_filter.format(prefix=alice[:9], suffix=alice[-13:], id=alice)

    assert search.search(UserRecord, filter=scim_filter)[0] == total


def test_an_integer_too_large_for_its_field_matches_nothing(stored):
    """A group identifier is an integer, which the database cannot compare beyond its range."""
    large = "9" * 30

    assert stored.search(GroupRecord, filter=f'id eq "{large}"')[0] == 0
    assert stored.search(GroupRecord, filter=f'id ne "{large}"')[0] == 3
    assert stored.search(UserRecord, filter=f'groups.value eq "{large}"')[0] == 0


def test_entries_sort_on_the_entry_read_first():
    """The entries come in the order of their manager, for the sort as for the response."""
    users = users_mapping({"phoneNumbers": Many("phones", {"value": "value"})})
    search = Search(users)
    UserRecord.objects.exclude(user_name__in=["alice", "Bob"]).delete()
    alice = UserRecord.objects.get(user_name="alice")
    bob = UserRecord.objects.get(user_name="Bob")
    PhoneRecord.objects.bulk_create(
        [
            PhoneRecord(user=alice, value="1"),
            PhoneRecord(user=alice, value="9"),
            PhoneRecord(user=bob, value="5"),
        ]
    )

    _, ids = search.search(UserRecord, sort_by="phoneNumbers")

    assert ids == [str(bob.pk), str(alice.pk)]
    alice_read = next(r for r in search.resources(UserRecord) if r.id == str(alice.pk))
    assert [phone.value for phone in alice_read.phone_numbers] == ["9", "1"]


def test_a_simple_multi_valued_attribute_is_filtered_and_sorted_on_its_values():
    """Within a selection of tags, value is the tag itself, and nothing else exists."""
    tagged = tagged_mapping()
    tagged._bind(tagged._targets({}))
    for name, tags in (("ann", ["red", "Blue"]), ("ben", ["green"]), ("cid", [])):
        save(
            tagged,
            tagged.model.model_validate({"userName": name, TAGGED: {"tags": tags}}),
            TaggedUser(),
        )
    search = Search(vendor="other")
    search.users = tagged
    resources = [
        to_scim(tagged, record) for record in tagged._queryset(collections(tagged))
    ]

    for scim_filter in (
        f'{TAGGED}:tags eq "blue"',
        f'{TAGGED}:tags[value sw "g"]',
        f'{TAGGED}:tags[other eq "x"]',
        f"{TAGGED}:tags pr",
    ):
        expected = ScimFilter[tagged.model](scim_filter)
        _, ids = search.search(UserRecord, filter=scim_filter)
        assert sorted(ids) == sorted(r.id for r in resources if expected.match(r))

    search_request = SearchRequest(sort_by=f"{TAGGED}:tags")
    _, ids = search.search(UserRecord, sort_by=f"{TAGGED}:tags")
    by_id = sorted(resources, key=lambda r: r.id)
    assert ids == [r.id for r in search_request.sort(by_id)]


def test_every_readable_collection_is_returned_without_parameters():
    """A response without attributes keeps every collection the mapping reads."""
    users, _ = bound()

    assert [c.binding.urn for c in users._returned_collections()] == [
        c.binding.urn for c in collections(users)
    ]


def test_a_computed_attribute_is_filtered_on_its_expression():
    """Filters compare the value clients read, in the records and in the links."""
    users = users_mapping(
        {
            "displayName": Computed(
                Concat("user_name", Value("!"), output_field=TextField())
            )
        }
    )
    search = Search(users)

    _, ids = search.search(UserRecord, filter='displayName eq "BOB!"')
    _, groups = search.search(GroupRecord, filter='members.display eq "bob!"')

    assert ids == [str(UserRecord.objects.get(user_name="Bob").pk)]
    assert groups == [str(GroupRecord.objects.get(display_name="Staff").pk)]
    assert {m.display for m in search.resources(GroupRecord)[1].members} == {
        "alice!",
        "carol!",
        "Staff",
    }


@pytest.mark.parametrize(
    "scim_filter", ["active eq true", "active ne true", "active pr"]
)
def test_a_computed_attribute_that_is_not_a_string_is_filtered_on_its_expression(
    scim_filter,
):
    """A user is active when it has a title, as the expression computes it."""
    has_title = ExpressionWrapper(Q(title__isnull=False), output_field=BooleanField())
    search = Search(users_mapping({"active": Computed(has_title)}))

    _, ids = search.search(UserRecord, filter=scim_filter)

    assert sorted(ids) == sorted(search.matching(UserRecord, scim_filter))


@pytest.mark.parametrize(
    "scim_filter",
    [
        'password eq "x"',
        "password pr",
        'groups[value eq "x"] and password pr',
        'phoneNumbers[value eq "x"]',
        "phoneNumbers pr",
        "addresses pr",
    ],
)
def test_a_filter_on_an_attribute_filters_cannot_read_is_refused(stored, scim_filter):
    """A write-only attribute, or one the mapping does not store, cannot be filtered on."""
    with pytest.raises(InvalidFilterException):
        stored.search(UserRecord, filter=scim_filter)


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
@pytest.mark.parametrize(
    "sort_by",
    [*SEARCH_SORTS, "locale", "id", "nonexistent", "password", "preferredLanguage"],
)
def test_a_sort_orders_the_users_as_search_request_sort(stored, sort_by, sort_order):
    """The database orders the users as SearchRequest.sort does, ties by identifier."""
    search_request = SearchRequest(sort_by=sort_by, sort_order=sort_order)
    resources = sorted(stored.resources(UserRecord), key=lambda r: r.id)

    _, ids = stored.search(UserRecord, sort_by=sort_by, sort_order=sort_order)

    assert ids == [r.id for r in search_request.sort(resources)]


@pytest.mark.parametrize("sort_by", ["groups", "groups.display"])
def test_a_sort_on_an_attribute_sorts_cannot_read_is_refused(stored, sort_by):
    """A link cannot be sorted on."""
    with pytest.raises(InvalidPathException):
        stored.search(UserRecord, sort_by=sort_by)


def test_a_page_is_cut_after_filtering_and_sorting(stored):
    """The total counts every match, and the page holds count of them from startIndex."""
    total, ids = stored.search(
        UserRecord, filter="userName pr", sort_by="userName", start_index=2, count=2
    )

    assert total == 4
    assert ids == [
        str(UserRecord.objects.get(user_name=name).pk) for name in ("Bob", "carol")
    ]


def test_a_page_without_count_holds_every_record_from_start_index(stored):
    _, ids = stored.search(UserRecord, sort_by="userName", start_index=4)

    assert ids == [str(UserRecord.objects.get(user_name="dave_%").pk)]


def test_a_search_loads_the_collections_the_response_keeps(
    stored, django_assert_num_queries
):
    """The page is read in one query per kept collection, whatever its size."""
    search_request = SearchRequest(attributes=["userName", "emails"])
    kept = stored.users._returned_collections(search_request)
    _, page = _search(stored.users, search_request, stored.comparator)

    with django_assert_num_queries(2):
        resources = [
            _to_scim(stored.users, record, "User", stored.users.model, {}, kept)
            for record in page
        ]

    assert sorted(len(r.emails or []) for r in resources) == [0, 1, 1, 2]
    assert all(r.groups is None for r in resources)
