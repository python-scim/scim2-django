import pytest
from django.db.models.signals import post_save
from scim2_models import EnterpriseUser
from scim2_models import InvalidValueException
from scim2_models import PreconditionFailedException
from scim2_models import ScimFilter
from scim2_models import SearchRequest
from scim2_models import User
from scim2_server.utils import load_default_provider

from scim2_django.storage import DjangoStorage
from scim2_django.storage import Many
from scim2_django.storage import ResourceMapping

from .mappings import ENTERPRISE
from .mappings import groups_mapping
from .mappings import users_mapping
from .models import UserCard
from .models import UserRecord
from .models import UserScim
from .test_storage import Server

pytestmark = pytest.mark.django_db

BESIDE = {
    "externalId": "scim__external_id",
    f"{ENTERPRISE}:division": "scim__division",
}
"""Attributes stored in the table beside the users."""


@pytest.fixture
def server():
    return Server(mappings={"User": users_mapping(BESIDE)})


def create(server, **values):
    return server.storage.create(server.users, server.user_model(**values))


def division(resource):
    return resource.model_dump().get(ENTERPRISE, {}).get("division")


def with_division(resource, value):
    """Return a copy of a user with another division, or without one."""
    data = resource.model_dump()
    data[ENTERPRISE] = {"division": value} if value is not None else None
    return type(resource).model_validate(
        {key: value for key, value in data.items() if value is not None}
    )


def test_a_user_without_related_record_has_no_such_value(server):
    """A user created without these attributes gets no record beside it."""
    user = create(server, user_name="bjensen")

    assert user.external_id is None
    assert division(user) is None
    assert not UserScim.objects.exists()


def test_the_first_value_creates_the_related_record(server):
    """The record beside the user is created with the user, and keeps the defaults of the other fields."""
    user = create(server, user_name="bjensen", external_id="42")

    scim = UserScim.objects.get()
    assert user.external_id == "42"
    assert (scim.user_id, scim.division, scim.level) == (
        UserRecord.objects.get().pk,
        "",
        0,
    )


def test_an_update_writes_the_related_record(server):
    """The values change in the record beside the user, which a missing value empties."""
    user = create(server, user_name="bjensen", external_id="42")
    user = with_division(user, "R&D")
    user.external_id = None

    updated = server.storage.update(server.users, user)

    scim = UserScim.objects.get()
    assert updated.external_id is None
    assert division(updated) == "R&D"
    assert (scim.external_id, scim.division) == (None, "R&D")


def test_an_update_creates_the_related_record_of_a_user_without_one(server):
    user = create(server, user_name="bjensen")
    user.external_id = "42"

    assert server.storage.update(server.users, user).external_id == "42"
    assert UserScim.objects.get().external_id == "42"


def test_removing_a_text_value_writes_an_empty_string(server):
    """The division is a text field without null, as Django recommends."""
    user = create(server, user_name="bjensen")
    user = server.storage.update(server.users, with_division(user, "R&D"))

    updated = server.storage.update(server.users, with_division(user, None))

    assert division(updated) is None
    assert UserScim.objects.get().division == ""


def test_deleting_a_user_deletes_its_related_record(server):
    user = create(server, user_name="bjensen", external_id="42")

    server.storage.delete(server.users, user.id)

    assert not UserScim.objects.exists()


@pytest.fixture
def users(server):
    """Create users, two of them without a record beside them."""
    for user_name, external_id in [("a", "2"), ("b", None), ("c", "1"), ("d", None)]:
        create(server, user_name=user_name, external_id=external_id)

    create(server, user_name="e", external_id="x")
    UserScim.objects.filter(external_id="x").update(external_id=None)
    return server


@pytest.mark.parametrize(
    "scim_filter",
    ['externalId eq "1"', "externalId pr", "not (externalId pr)", 'externalId ne "2"'],
)
def test_a_filter_on_a_related_field_keeps_the_users_kept_in_memory(users, scim_filter):
    """A user without record beside it has no value, as one with an empty record."""
    everyone = users.storage.search([users.users], SearchRequest()).resources
    evaluator = ScimFilter[users.user_model](scim_filter)

    page = users.storage.search([users.users], SearchRequest(filter=scim_filter))

    expected = [r.user_name for r in everyone if evaluator.match(r)]
    assert sorted(r.user_name for r in page.resources) == sorted(expected)


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
def test_a_sort_on_a_related_field_orders_as_search_request_sort(users, sort_order):
    """Users without value come last when ascending and first when descending."""
    search_request = SearchRequest(sort_by="externalId", sort_order=sort_order)
    everyone = sorted(
        users.storage.search([users.users], SearchRequest()).resources,
        key=lambda r: r.id,
    )

    page = users.storage.search([users.users], search_request)

    assert [r.user_name for r in page.resources] == [
        r.user_name for r in search_request.sort(everyone)
    ]


def test_a_page_reads_the_related_records_along_with_the_users(
    users, django_assert_num_queries
):
    """The records beside the users come in the query of the page."""
    search_request = SearchRequest(attributes=["externalId"])
    collections = users.storage.mappings["User"]._returned_collections(search_request)
    records = users.storage.mappings["User"]._queryset(collections)

    with django_assert_num_queries(1):
        values = sorted(
            str(record.scim.external_id)
            for record in records
            if hasattr(record, "scim")
        )

    assert values == ["1", "2", "None"]


def mapping(value, path="externalId"):
    return users_mapping({path: value})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("externalId", "manager__user_name"),
        ("externalId", "groups__display_name"),
        ("externalId", "emails__value"),
        ("externalId", "scim__user__user_name"),
        ("id", "scim__user_id"),
    ],
    ids=[
        "foreign key",
        "many-to-many",
        "reverse foreign key",
        "two relations",
        "id",
    ],
)
def test_a_field_of_another_model_is_reached_through_one_reverse_one_to_one(
    path, value
):
    """Writing through a foreign key would change the value of every record sharing the related one."""
    errors = [message.id for message in mapping(value, path).check()]

    assert "scim2_django.E010" in errors


def test_an_entry_field_is_not_reached_through_a_relation():
    """The entries of a Many hold their values themselves."""
    users = users_mapping({"emails": Many("emails", {"value": "user__user_name"})})

    assert [message.id for message in users.check()] == ["scim2_django.E010"]


def test_a_related_model_needs_a_default_for_the_fields_the_mapping_leaves_out():
    """A card needs a number, which the mapping does not give."""
    errors = [message.msg for message in mapping("card__note").check()]

    assert len(errors) == 1
    assert "'number' needs a value" in errors[0]


@pytest.mark.parametrize("value", ["nope__external_id", "scim__nope"])
def test_an_unknown_relation_or_related_field_is_reported(value):
    assert [message.id for message in mapping(value).check()] == ["scim2_django.E001"]


def test_a_mapping_through_a_relation_passes_the_checks():
    users = ResourceMapping(
        User[EnterpriseUser],
        UserRecord,
        {"id": "id", "userName": "user_name", **BESIDE},
        version="version",
    )

    assert users.check() == []


def test_a_member_displays_a_name_held_beside_it():
    """The display of a member comes from the record beside the user, read along with it."""
    server = Server(mappings={"User": users_mapping({"displayName": "scim__division"})})
    user = create(server, user_name="bjensen", display_name="Babs")
    group = server.group_model.model_validate(
        {"displayName": "Admins", "members": [{"value": user.id}]}
    )

    created = server.storage.create(server.groups, group)

    assert [member.display for member in created.members] == ["Babs"]


def test_a_related_record_cannot_be_created_without_its_required_values():
    """A card needs a number, which the client did not give."""
    server = Server(
        mappings={
            "User": users_mapping({"nickName": "card__note", "active": "card__number"})
        }
    )

    with pytest.raises(InvalidValueException, match="active"):
        create(server, user_name="bjensen", nick_name="Babs")

    user = create(server, user_name="jsmith", nick_name="Smithy", active=True)
    assert (user.nick_name, user.active) == ("Smithy", True)
    assert UserCard.objects.get().number == 1


def test_a_new_user_keeps_the_defaults_of_a_related_record_created_beforehand(
    server,
):
    """A related record created by a signal keeps the values the client did not give."""

    def create_scim(sender, instance, **kwargs):
        UserScim.objects.create(user=instance, division="Sales")

    post_save.connect(create_scim, sender=UserRecord)
    try:
        user = create(server, user_name="bjensen", external_id="42")
    finally:
        post_save.disconnect(create_scim, sender=UserRecord)

    assert user.external_id == "42"
    assert division(user) == "Sales"


def test_a_write_reads_the_related_record_again_once_locked():
    """Another transaction changed the record beside the user while this one waited for the lock.

    The hash of the user then covers the new division, so the write is refused.
    """

    class Waiting(DjangoStorage):
        def _lock_related(self, relations, record):
            UserScim.objects.update(division="Sales")
            super()._lock_related(relations, record)

    provider = load_default_provider()
    users = next(rt for rt in provider.resource_types if rt.id == "User")
    mappings = {
        "User": users_mapping(BESIDE, version=None),
        "Group": groups_mapping(version=None),
    }
    storage = Waiting(mappings, provider)
    user = storage.create(
        users, provider.model_for(users)(user_name="bjensen", external_id="42")
    )

    with pytest.raises(PreconditionFailedException):
        storage.update(users, user, expected_version=user.meta.version)


def test_a_relation_is_queried_by_its_query_name():
    """The badges are queried as badge_record, and read as badge."""
    server = Server(
        mappings={"User": users_mapping({"nickName": "badge_record__label"})}
    )
    user = create(server, user_name="bjensen", nick_name="Babs")

    page = server.storage.search(
        [server.users], SearchRequest(filter='nickName eq "Babs"')
    )

    assert [r.id for r in page.resources] == [user.id]
    assert server.storage.get(server.users, user.id).nick_name == "Babs"


DATED = {"meta.created": "scim__created", "meta.lastModified": "scim__last_modified"}
"""Dates stored in the table beside the users."""


def test_a_new_user_gets_its_dates_beside_it():
    """The dates make each write create the record beside the user."""
    server = Server(mappings={"User": users_mapping(DATED)})

    user = create(server, user_name="bjensen")

    scim = UserScim.objects.get()
    assert user.meta.created == scim.created == scim.last_modified
    assert user.meta.last_modified == scim.last_modified


def test_an_update_dates_the_record_beside_the_user_it_creates():
    """A user created outside SCIM has no creation date: its first SCIM write creates the record beside it."""
    server = Server(mappings={"User": users_mapping(DATED)})
    record = UserRecord.objects.create(user_name="bjensen", preferred_language="fr")
    user = server.storage.get(server.users, str(record.pk))

    updated = server.storage.update(server.users, user)

    scim = UserScim.objects.get()
    assert user.meta.last_modified is None
    assert (scim.created, updated.meta.created) == (None, None)
    assert updated.meta.last_modified == scim.last_modified


def test_an_update_changes_the_modification_date_beside_the_user():
    server = Server(mappings={"User": users_mapping(DATED)})
    user = create(server, user_name="bjensen")

    updated = server.storage.update(server.users, user)

    assert updated.meta.last_modified > user.meta.last_modified
    assert updated.meta.created == user.meta.created


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
def test_a_sort_on_a_date_beside_the_users_orders_as_search_request_sort(sort_order):
    """Users created outside SCIM have no modification date."""
    server = Server(mappings={"User": users_mapping(DATED)})
    for user_name in ["a", "b"]:
        create(server, user_name=user_name)
    UserRecord.objects.create(user_name="c", preferred_language="fr")
    search_request = SearchRequest(sort_by="meta.lastModified", sort_order=sort_order)
    everyone = sorted(
        server.storage.search([server.users], SearchRequest()).resources,
        key=lambda r: r.id,
    )

    page = server.storage.search([server.users], search_request)

    assert [r.user_name for r in page.resources] == [
        r.user_name for r in search_request.sort(everyone)
    ]


def test_a_date_beside_the_users_needs_a_date_field():
    errors = [m.id for m in mapping("scim__level", "meta.created").check()]

    assert errors == ["scim2_django.E006"]


def test_dates_split_between_the_user_and_the_record_beside_it():
    """The creation date stays on the user, and the modification date goes beside it."""
    server = Server(
        mappings={"User": users_mapping({"meta.lastModified": "scim__last_modified"})}
    )

    user = create(server, user_name="bjensen")

    scim = UserScim.objects.get()
    assert user.meta.created == UserRecord.objects.get().created
    assert user.meta.last_modified == scim.last_modified
    assert scim.created is None


def test_a_record_holding_a_date_cannot_need_a_value_from_the_client():
    """Each write creates the card to date it, so its number would be required on every write."""
    users = users_mapping(
        {"active": "card__number", "meta.lastModified": "card__issued"}
    )

    assert [m.id for m in users.check()] == ["scim2_django.E010"]


def test_a_record_holding_dates_can_hold_optional_values():
    """The external identifier and the division need no value, so each write can create the record."""
    assert users_mapping(BESIDE | DATED).check() == []
