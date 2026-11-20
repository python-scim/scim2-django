import datetime

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.db.models import F
from scim2_models import Group
from scim2_models import InvalidValueException
from scim2_models import NotFoundException
from scim2_models import PreconditionFailedException
from scim2_models import ResourceType
from scim2_models import ScimPolicy
from scim2_models import ScimProvider
from scim2_models import SearchRequest
from scim2_models import UniquenessException
from scim2_server.utils import load_default_provider
from scim2_server.utils import load_default_service_provider_config

from scim2_django.storage import DjangoStorage

from .mappings import groups_mapping
from .mappings import users_mapping
from .models import EmailRecord
from .models import UserRecord

pytestmark = pytest.mark.django_db

NOW = datetime.datetime(2026, 11, 20, 8, 0, tzinfo=datetime.UTC)


def strict_key(binding, value):
    """Fold the case, and refuse the strings holding an exclamation mark."""
    if "!" in value:
        raise ValueError("'!' cannot be compared")

    return value if binding.case_exact else value.casefold()


class Server:
    """A storage of the users and the groups, with its resource types and models."""

    def __init__(self, storage_class=DjangoStorage, policy=None, mappings=None):
        provider = load_default_provider()
        if policy is not None:
            provider = ScimProvider.from_discovery(
                [*provider.schemas],
                provider.resource_types,
                config=load_default_service_provider_config(),
                policy=policy,
            )

        self.users = next(rt for rt in provider.resource_types if rt.id == "User")
        self.groups = next(rt for rt in provider.resource_types if rt.id == "Group")
        self.user_model = provider.model_for(self.users)
        self.group_model = provider.model_for(self.groups)
        mappings = {"User": users_mapping(), "Group": groups_mapping()} | (
            mappings or {}
        )
        self.storage = storage_class(mappings, provider)

    def create_user(self, user_name):
        return self.storage.create(self.users, self.user_model(user_name=user_name))

    def create_group(self, members):
        group = self.group_model.model_validate(
            {"displayName": "Team", "members": members}
        )
        return self.storage.create(self.groups, group)


@pytest.fixture
def server():
    return Server()


def test_a_search_on_no_resource_type_finds_nothing(server):
    page = server.storage.search([], SearchRequest())

    assert (page.total, page.resources) == (0, [])


def test_an_identifier_the_field_cannot_hold_is_not_found(server):
    """A user identifier is a UUID."""
    with pytest.raises(NotFoundException):
        server.storage.get(server.users, "not-a-uuid")


def test_a_refused_mapping_binds_no_mapping():
    """A Link to a resource type without mapping refuses every operation."""
    provider = load_default_provider()
    users = next(rt for rt in provider.resource_types if rt.id == "User")
    storage = DjangoStorage({"User": users_mapping()}, provider)

    with pytest.raises(ImproperlyConfigured, match="'Group'"):
        storage.search([users], SearchRequest())

    assert all(c.target is None for c in storage.mappings["User"]._all_collections())


class Concurrent(DjangoStorage):
    """A storage where another transaction writes each record after it is read."""

    def _check_version(self, stored, expected_version):
        UserRecord.objects.update(version=F("version") + 1)


def test_a_write_since_the_record_was_read_is_refused():
    """The write of the other transaction is not overwritten, nor deleted."""
    server = Server(Concurrent)
    user = server.create_user("bjensen")

    with pytest.raises(PreconditionFailedException):
        server.storage.update(server.users, user)

    with pytest.raises(PreconditionFailedException):
        server.storage.delete(server.users, user.id, expected_version=user.meta.version)

    assert UserRecord.objects.filter(user_name="bjensen").exists()


class Racing(DjangoStorage):
    """A storage whose first uniqueness check of each write misses a value taken in the meantime."""

    checks = 0

    def _check_uniqueness(self, mapping, resource, stored):
        self.checks += 1
        if self.checks % 2 == 0:
            super()._check_uniqueness(mapping, resource, stored)


def test_a_unique_value_taken_in_the_meantime_is_refused():
    """The database refuses the value, and the storage tells the client why."""
    server = Server(Racing)
    server.create_user("bjensen")

    with pytest.raises(UniquenessException):
        server.create_user("BJENSEN")

    assert UserRecord.objects.count() == 1


class Blind(DjangoStorage):
    """A storage that never checks uniqueness."""

    def _check_uniqueness(self, mapping, resource, stored):
        pass


def test_another_integrity_error_goes_up():
    """An error the uniqueness does not explain is not hidden."""
    server = Server(Blind)
    server.create_user("bjensen")

    with pytest.raises(IntegrityError):
        server.create_user("bjensen")


def test_a_link_to_an_unknown_resource_is_refused(server):
    with pytest.raises(InvalidValueException, match="unknown resource"):
        server.create_group([{"value": "00000000-0000-0000-0000-000000000000"}])


def test_a_member_is_linked_once(server):
    """A user given twice is a member once."""
    user = server.create_user("bjensen")

    group = server.create_group(
        [{"value": user.id, "type": "user"}, {"value": user.id}]
    )

    assert [(m.value, m.type) for m in group.members] == [(user.id, "User")]


def test_a_member_of_another_type_is_refused(server):
    """A user cannot be given as a group."""
    user = server.create_user("bjensen")

    with pytest.raises(InvalidValueException, match="not of type 'Group'"):
        server.create_group([{"value": user.id, "type": "Group"}])


@pytest.mark.filterwarnings("ignore:The postgresql storage cannot compare")
def test_a_type_the_policy_cannot_compare_is_refused():
    server = Server(policy=ScimPolicy(comparison_key=strict_key))
    user = server.create_user("bjensen")

    with pytest.raises(InvalidValueException, match="not of type 'User!'"):
        server.create_group([{"value": user.id, "type": "User!"}])


def test_a_group_resource_type_without_provider():
    """Without provider, the storage returns instances of the models of the mappings."""
    storage = DjangoStorage({"User": users_mapping(), "Group": groups_mapping()})
    groups = ResourceType.from_resource(Group)

    group = storage.create(groups, Group(display_name="Team"))

    assert type(group) is Group
    assert group.members is None


def test_a_unique_value_written_by_another_application_can_stay(server):
    """A user whose name differs from another one by its case only can still be changed."""
    server.create_user("alice")
    record = UserRecord.objects.create(
        user_name="Alice", created=NOW, last_modified=NOW
    )
    other = server.storage.get(server.users, str(record.pk))
    other.display_name = "Other Alice"

    assert server.storage.update(server.users, other).display_name == "Other Alice"

    other.user_name = "ALICE"
    with pytest.raises(UniquenessException):
        server.storage.update(server.users, other)


def test_a_new_resource_cannot_take_a_value_by_giving_the_identifier_of_its_holder(
    server,
):
    """The identifier of a new resource is not the one of the resource holding the value."""
    user = server.create_user("bjensen")

    with pytest.raises(UniquenessException):
        server.storage.create(
            server.users, server.user_model(id=user.id, user_name="bjensen")
        )


def test_the_identifier_of_another_resource_takes_nothing(server):
    """The storage never writes id, so a new resource giving the id of another one gets its own."""
    user = server.create_user("bjensen")

    created = server.storage.create(
        server.users, server.user_model(id=user.id, user_name="jsmith")
    )

    assert created.id != user.id
    assert UserRecord.objects.count() == 2


def test_a_resource_type_without_mapping_is_refused():
    """The provider serves groups, which the storage does not map."""
    provider = load_default_provider()
    users = next(rt for rt in provider.resource_types if rt.id == "User")
    mappings = {"User": users_mapping(), "Group": groups_mapping()}
    storage = DjangoStorage({"User": mappings["User"]}, provider)

    with pytest.raises(ImproperlyConfigured, match="'Group'"):
        storage.get(users, "x")

    storage = DjangoStorage(mappings)
    devices = ResourceType(
        id="Device", name="Device", endpoint="/Devices", schema_="urn:example:Device"
    )
    with pytest.raises(ImproperlyConfigured, match="'Device'"):
        storage.get(devices, "x")


@pytest.mark.django_db(databases=["default", "other"])
def test_the_storage_reads_and_writes_the_database_it_is_given(server):
    """The records, their entries and the searches are in the other database."""
    storage = DjangoStorage(server.storage.mappings, using="other")
    user = server.user_model(
        user_name="bjensen", emails=[{"value": "bjensen@example.com"}]
    )

    user = storage.create(server.users, user)

    assert UserRecord.objects.using("other").count() == 1
    assert EmailRecord.objects.using("other").count() == 1
    assert not UserRecord.objects.exists()
    page = storage.search([server.users], SearchRequest(filter="emails pr"))
    assert [found.id for found in page.resources] == [user.id]
