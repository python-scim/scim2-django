import pytest
from scim2_models import PreconditionFailedException
from scim2_models import ResponseParameters
from scim2_models import SearchRequest
from scim2_server.utils import load_default_provider

from scim2_django.storage import DjangoStorage

from .mappings import groups_mapping
from .mappings import users_mapping
from .models import UserRecord

pytestmark = pytest.mark.django_db


class Server:
    """A storage of users and groups versioned by the hash of their resources."""

    def __init__(self, changes=None):
        provider = load_default_provider()
        self.users = next(rt for rt in provider.resource_types if rt.id == "User")
        self.groups = next(rt for rt in provider.resource_types if rt.id == "Group")
        self.user_model = provider.model_for(self.users)
        self.group_model = provider.model_for(self.groups)
        mappings = {
            "User": users_mapping(changes, version=None),
            "Group": groups_mapping(changes, version=None),
        }
        self.storage = DjangoStorage(mappings, provider)

    def create_user(self, user_name):
        return self.storage.create(self.users, self.user_model(user_name=user_name))

    def get_user(self, user_id, **parameters):
        response_parameters = ResponseParameters(**parameters) if parameters else None
        return self.storage.get(
            self.users, user_id, response_parameters=response_parameters
        )


@pytest.fixture
def server():
    return Server()


def test_the_version_is_the_hash_of_the_resource(server):
    """Reading a resource twice gives the same version, a weak ETag of 32 hexadecimal digits."""
    user = server.create_user("bjensen")

    assert server.get_user(user.id).meta.version == user.meta.version
    assert user.meta.version.startswith('W/"')
    assert len(user.meta.version) == len('W/""') + 32


def test_a_write_changes_the_version_through_the_modification_date(server):
    """The modification date is part of the resource, so any write changes the hash."""
    user = server.create_user("bjensen")

    rewritten = server.storage.update(server.users, user)

    assert rewritten.meta.last_modified > user.meta.last_modified
    assert rewritten.meta.version != user.meta.version


def test_a_write_changing_nothing_keeps_the_version_without_dates():
    """Without a modification date, a resource written back as it is keeps its hash."""
    server = Server({"meta.created": None, "meta.lastModified": None})
    user = server.create_user("bjensen")

    rewritten = server.storage.update(server.users, user)
    user.display_name = "Babs"
    changed = server.storage.update(server.users, user)

    assert rewritten.meta.version == user.meta.version
    assert changed.meta.version != user.meta.version


def test_a_new_group_changes_the_version_of_its_members(server):
    """The groups of a user are part of the user, although its record does not change."""
    user = server.create_user("bjensen")
    group = server.group_model.model_validate(
        {"displayName": "Admins", "members": [{"value": user.id}]}
    )

    server.storage.create(server.groups, group)

    assert server.get_user(user.id).meta.version != user.meta.version


def test_a_write_outside_the_storage_changes_the_version(server):
    """The hash is computed from the record, whatever wrote it."""
    user = server.create_user("bjensen")

    UserRecord.objects.update(display_name="Babs")

    assert server.get_user(user.id).meta.version != user.meta.version


def test_the_version_covers_the_attributes_a_response_leaves_out(server):
    """A response without the groups gives the version of the whole user."""
    user = server.create_user("bjensen")

    partial = server.get_user(user.id, excluded_attributes=["groups"])

    assert partial.meta.version == user.meta.version


def test_a_write_on_an_outdated_version_is_refused(server):
    """The client read the user before another write, so its If-Match does not hold."""
    user = server.create_user("bjensen")
    UserRecord.objects.update(display_name="Babs")

    with pytest.raises(PreconditionFailedException):
        server.storage.update(server.users, user, expected_version=user.meta.version)

    with pytest.raises(PreconditionFailedException):
        server.storage.delete(server.users, user.id, expected_version=user.meta.version)

    assert UserRecord.objects.get().display_name == "Babs"


def test_a_write_on_the_current_version_is_stored(server):
    user = server.create_user("bjensen")
    user.display_name = "Babs"

    updated = server.storage.update(
        server.users, user, expected_version=user.meta.version
    )
    server.storage.delete(server.users, user.id, expected_version=updated.meta.version)

    assert updated.display_name == "Babs"
    assert not UserRecord.objects.exists()


def test_a_new_password_changes_the_version():
    """The password is never read, yet a client writing it again holds an outdated version."""
    server = Server({"meta.created": None, "meta.lastModified": None})
    user = server.create_user("bjensen")
    user.password = "t1meMa$heen"

    updated = server.storage.update(server.users, user)

    assert updated.password is None
    assert updated.meta.version != user.meta.version


def test_the_version_depends_on_the_secret_key(server, settings):
    """Without the secret key, a version tells nothing about the values it covers."""
    user = server.create_user("bjensen")

    settings.SECRET_KEY = "another-secret"

    assert server.get_user(user.id).meta.version != user.meta.version


def test_groups_of_the_same_name_come_in_the_order_of_their_creation(server):
    """The primary key orders the groups their ordering leaves tied, so the hash of the user never changes."""
    user = server.create_user("bjensen")
    for _ in range(2):
        group = server.group_model.model_validate(
            {"displayName": "Admins", "members": [{"value": user.id}]}
        )
        server.storage.create(server.groups, group)

    groups = server.get_user(user.id).groups
    found = server.storage.search([server.users], SearchRequest()).resources[0]

    assert [g.value for g in groups] == sorted((g.value for g in groups), key=int)
    assert found.meta.version == server.get_user(user.id).meta.version
