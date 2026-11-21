import pytest
from django.db.models import F
from scim2_models import Group
from scim2_models import InvalidFilterException
from scim2_models import InvalidValueException
from scim2_models import Mutability
from scim2_models import MutabilityException
from scim2_models import ScimFilter
from scim2_models import SearchRequest
from scim2_models import User

from scim2_django.storage import Attribute
from scim2_django.storage import Computed
from scim2_django.storage import Entry
from scim2_django.storage import ResourceMapping

from .mappings import users_mapping
from .models import GroupRecord
from .models import UserRecord
from .test_storage import Server

pytestmark = pytest.mark.django_db

EMAILS = Entry({"value": "email"}, type="work", primary=True)


@pytest.fixture
def server():
    return Server(mappings={"User": users_mapping({"emails": EMAILS})})


def create(server, user_name, emails=None):
    user = server.user_model.model_validate(
        {"userName": user_name, **({"emails": emails} if emails is not None else {})}
    )
    return server.storage.create(server.users, user)


def work(value, **others):
    return {"value": value, "type": "work", **others}


def dump(resource):
    return resource.model_dump(mode="json").get("emails")


def test_the_entry_holds_the_field_and_the_constants(server):
    user = create(server, "bjensen", [work("bjensen@example.com")])

    assert dump(user) == [
        {"value": "bjensen@example.com", "type": "work", "primary": True}
    ]
    assert UserRecord.objects.get().email == "bjensen@example.com"


def test_a_resource_without_value_has_no_entry(server):
    """The field keeps its default, an empty string, which is no value."""
    user = create(server, "bjensen")

    assert dump(user) is None
    assert UserRecord.objects.get().email == ""


def test_an_update_without_entry_empties_the_field(server):
    user = create(server, "bjensen", [work("bjensen@example.com")])
    user.emails = None

    updated = server.storage.update(server.users, user)

    assert dump(updated) is None
    assert UserRecord.objects.get().email == ""


@pytest.mark.parametrize(
    "entry",
    [
        {"value": "bjensen@example.com"},
        {"value": "bjensen@example.com", "type": "Work"},
        {"value": "bjensen@example.com", "type": "work", "primary": True},
    ],
    ids=["without constants", "constant of another case", "every constant"],
)
def test_an_entry_may_leave_out_the_constants(server, entry):
    """Entra sends emails[type eq "work"].value, without primary."""
    user = create(server, "bjensen", [entry])

    assert dump(user)[0]["value"] == "bjensen@example.com"


@pytest.mark.parametrize(
    ("emails", "message"),
    [
        (
            [work("bjensen@example.com"), work("babs@example.com")],
            "one entry at most",
        ),
        ([{"value": "bjensen@example.com", "type": "home"}], "can only be 'work'"),
        ([work("bjensen@example.com", primary=False)], "can only be True"),
        ([work("bjensen@example.com", display="Babs")], "not stored"),
    ],
    ids=["several entries", "another type", "not primary", "unmapped sub-attribute"],
)
def test_an_entry_the_fields_cannot_hold_is_refused(server, emails, message):
    with pytest.raises(InvalidValueException, match=message):
        create(server, "bjensen", emails)

    assert not UserRecord.objects.exists()


@pytest.fixture
def users(server):
    """Create users, one of them without email."""
    for user_name, email in [
        ("a", "alice@example.com"),
        ("b", None),
        ("c", "carol@example.org"),
    ]:
        create(server, user_name, [work(email)] if email else None)

    return server


@pytest.mark.parametrize(
    "scim_filter",
    [
        'emails.value eq "alice@example.com"',
        'emails co "example.org"',
        'emails ne "alice@example.com"',
        "emails pr",
        "not (emails pr)",
        'emails.type eq "work"',
        'emails.type eq "home"',
        'emails.type ne "work"',
        "emails.type pr",
        "emails.primary eq true",
        'emails[type eq "work" and value sw "a"]',
        'emails[type eq "home"]',
        'not (emails[value ew ".org"])',
    ],
)
def test_a_filter_keeps_the_users_kept_in_memory(users, scim_filter):
    everyone = users.storage.search([users.users], SearchRequest()).resources
    evaluator = ScimFilter[users.user_model](scim_filter)

    page = users.storage.search([users.users], SearchRequest(filter=scim_filter))

    expected = [r.user_name for r in everyone if evaluator.match(r)]
    assert sorted(r.user_name for r in page.resources) == sorted(expected)


def test_a_selection_on_an_unmapped_sub_attribute_is_refused(users):
    with pytest.raises(InvalidFilterException):
        users.storage.search(
            [users.users], SearchRequest(filter='emails[display eq "x"]')
        )


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
def test_a_sort_on_the_entry_orders_as_search_request_sort(users, sort_order):
    search_request = SearchRequest(sort_by="emails", sort_order=sort_order)
    everyone = sorted(
        users.storage.search([users.users], SearchRequest()).resources,
        key=lambda r: r.id,
    )

    page = users.storage.search([users.users], search_request)

    assert [r.user_name for r in page.resources] == [
        r.user_name for r in search_request.sort(everyone)
    ]


def test_the_schema_holds_the_mapped_sub_attributes_and_the_constants():
    (schema, _) = users_mapping({"emails": EMAILS}).schemas()
    (emails,) = [a for a in schema.attributes if a.name == "emails"]

    assert [a.name for a in emails.sub_attributes] == ["value", "type", "primary"]


def test_an_entry_without_constants_stores_no_type():
    server = Server(
        mappings={"User": users_mapping({"emails": Entry({"value": "email"})})}
    )

    with pytest.raises(InvalidValueException, match="not stored"):
        create(server, "bjensen", [work("bjensen@example.com")])

    user = create(server, "bjensen", [{"value": "bjensen@example.com"}])
    assert dump(user) == [{"value": "bjensen@example.com"}]


def test_an_entry_of_several_fields_exists_when_one_has_a_value():
    emails = Entry({"value": "email", "display": "nick_name"}, type="work")
    server = Server(mappings={"User": users_mapping({"emails": emails})})
    create(server, "bjensen", [{"display": "Babs"}])

    page = server.storage.search(
        [server.users], SearchRequest(filter='emails.type eq "work"')
    )

    assert dump(page.resources[0]) == [{"display": "Babs", "type": "work"}]


@pytest.mark.parametrize(
    ("path", "entry", "message"),
    [
        ("userName", EMAILS, "not a multi-valued complex attribute"),
        ("emails.value", EMAILS, "not a multi-valued complex attribute"),
        ("emails", Entry({}), "maps no sub-attribute"),
        (
            "emails",
            Entry({"value": Computed(F("email"))}),
            "must be mapped to the name of a field, or an Attribute",
        ),
        ("emails", Entry({"value": "scim__email"}), "not of a related model"),
        (
            "emails",
            Entry({"value": "email", "type": "email"}, type="work"),
            "which is not a sub-attribute it leaves unmapped",
        ),
    ],
    ids=[
        "single-valued",
        "sub-attribute",
        "empty",
        "computed",
        "related field",
        "mapped constant",
    ],
)
def test_an_entry_that_does_not_fit_its_attribute_is_refused(path, entry, message):
    with pytest.raises(ValueError, match=message):
        users_mapping({"emails": None, path: entry})


def test_an_entry_gives_existing_sub_attributes_only():
    """The members of a group have no primary sub-attribute."""
    with pytest.raises(ValueError, match="which is not a sub-attribute"):
        ResourceMapping(
            Group,
            GroupRecord,
            {
                "id": "id",
                "members": Entry({"value": "display_name"}, primary=True),
            },
        )


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        (Entry({"value": "nope"}), "scim2_django.E001"),
        (Entry({"value": Attribute("email", readable=False)}), "scim2_django.E007"),
        (Entry({"value": "email", "primary": "version"}), "scim2_django.E007"),
        (Entry({"value": "email", "display": "lower_name"}), "scim2_django.E007"),
    ],
    ids=["unknown field", "write-only value", "field without null", "read-only value"],
)
def test_the_fields_of_an_entry_are_checked(entry, error):
    assert [message.id for message in users_mapping({"emails": entry}).check()] == [
        error
    ]


def test_a_mapping_with_an_entry_passes_the_checks():
    users = ResourceMapping(
        User, UserRecord, {"id": "id", "emails": EMAILS}, version="version"
    )

    assert users.check() == []


def test_an_entry_of_read_only_fields_is_read_only():
    """The value is a generated field, the lower case user name."""
    emails = Entry({"value": "lower_name"}, type="work")
    server = Server(mappings={"User": users_mapping({"emails": emails})})
    (schema, _) = server.storage.mappings["User"].schemas()
    (attribute,) = [a for a in schema.attributes if a.name == "emails"]

    user = create(server, "BJensen")
    rewritten = server.storage.update(server.users, user)

    assert dump(rewritten) == [{"value": "bjensen", "type": "work"}]
    assert {a.mutability for a in [attribute, *attribute.sub_attributes]} == {
        Mutability.read_only
    }
    with pytest.raises(MutabilityException):
        create(server, "jsmith", [{"value": "smithy"}])


def test_a_new_entry_keeps_the_defaults_of_the_fields_it_leaves_out():
    emails = Entry({"value": "email", "display": "user_type"})
    server = Server(mappings={"User": users_mapping({"emails": emails})})

    user = create(server, "bjensen", [{"value": "bjensen@example.com"}])

    assert dump(user) == [{"value": "bjensen@example.com", "display": "Employee"}]
