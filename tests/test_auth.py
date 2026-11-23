import pytest
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import User as AuthUser
from django.core.signals import request_finished
from django.core.signals import request_started
from django.core.wsgi import get_wsgi_application
from django.db import close_old_connections
from scim2_client.engines.wsgi import WSGISCIMClient
from scim2_models import EnterpriseUser
from scim2_models import InvalidValueException
from scim2_models import Mutability
from scim2_models import NotFoundException
from scim2_models import SearchRequest
from scim2_models import User
from scim2_tester import Status
from scim2_tester.testing import ScimServerContract

from examples.accounts import create_provider
from scim2_django.auth import group_mapping
from scim2_django.auth import user_mapping
from scim2_django.storage import DjangoStorage
from scim2_django.storage import Transformed

from .models import Member

pytestmark = pytest.mark.django_db


@pytest.mark.django_db
class TestAccounts(ScimServerContract):
    """The users and the groups of django.contrib.auth, served from end to end.

    The seed fixes the values the checks send, so that the expected failures
    always happen.
    """

    seed = 3330931774
    expected_results = {
        ("User", "check_add_attribute", "emails"): (
            Status.ERROR,
            "scim2-tester sends primary=false in a lone entry, which reads back as "
            "the primary one",
        ),
        ("User", "check_replace_attribute", "emails"): (
            Status.ERROR,
            "scim2-tester sends primary=false in a lone entry, which reads back as "
            "the primary one",
        ),
        ("User", "check_remove_attribute", "active"): (
            Status.ERROR,
            "is_active cannot hold no value, so the storage refuses to remove active",
        ),
    }

    @pytest.fixture
    def scim_client(self):
        client = WSGISCIMClient(
            get_wsgi_application(), base_url="http://localhost/accounts/scim/v2"
        )
        request_started.disconnect(close_old_connections)
        request_finished.disconnect(close_old_connections)
        yield client
        request_started.connect(close_old_connections)
        request_finished.connect(close_old_connections)


class Server:
    """A storage of the users and the groups of django.contrib.auth."""

    def __init__(self, users=None, groups=None):
        users = users or user_mapping()
        groups = groups or group_mapping()
        provider = create_provider(users, groups)
        self.users = next(rt for rt in provider.resource_types if rt.id == "User")
        self.groups = next(rt for rt in provider.resource_types if rt.id == "Group")
        self.user_model = provider.model_for(self.users)
        self.group_model = provider.model_for(self.groups)
        self.storage = DjangoStorage({"User": users, "Group": groups}, provider)

    def create_user(self, **values):
        return self.storage.create(self.users, self.user_model.model_validate(values))

    def create_group(self, name, members=()):
        group = self.group_model.model_validate(
            {"displayName": name, "members": [{"value": value} for value in members]}
        )
        return self.storage.create(self.groups, group)


@pytest.fixture
def server():
    return Server()


def test_a_user_holds_the_fields_of_the_user_model(server):
    user = server.create_user(
        userName="bjensen",
        name={"givenName": "Barbara", "familyName": "Jensen"},
        emails=[{"value": "bjensen@example.com", "type": "work"}],
        active=False,
    )

    record = AuthUser.objects.get()
    assert user.id == f"u{record.pk}"
    assert (record.username, record.first_name, record.last_name) == (
        "bjensen",
        "Barbara",
        "Jensen",
    )
    assert (record.email, record.is_active) == ("bjensen@example.com", False)
    assert user.meta.created == record.date_joined
    assert user.meta.last_modified is None
    assert user.emails[0].primary is True


def test_users_and_groups_never_share_an_id(server):
    """The user and the group have the same primary key."""
    AuthUser.objects.create(pk=7, username="bjensen")
    AuthGroup.objects.create(pk=7, name="Admins")
    group = server.storage.get(server.groups, "g7")

    group.members = [{"value": "u7"}]
    updated = server.storage.update(server.groups, group)

    assert [member.value for member in updated.members] == ["u7"]
    assert [g.value for g in server.storage.get(server.users, "u7").groups] == ["g7"]
    assert list(AuthUser.objects.get().groups.all()) == [AuthGroup.objects.get()]


def test_a_password_is_hashed_by_django(server):
    server.create_user(userName="bjensen", password="t1meMa$heen")

    record = AuthUser.objects.get()
    assert record.password != "t1meMa$heen"
    assert record.check_password("t1meMa$heen")


def test_a_user_created_without_password_cannot_log_in(server):
    """As with create_user, the user gets an unusable password."""
    server.create_user(userName="bjensen")

    assert not AuthUser.objects.get().has_usable_password()


def test_a_login_keeps_the_version_of_the_user(server):
    """The date of the last login is not part of the user."""
    user = server.create_user(userName="bjensen")

    AuthUser.objects.update(last_login="2026-11-21T12:00:00Z")

    assert server.storage.get(server.users, user.id).meta.version == user.meta.version


def test_a_new_member_changes_the_version_of_the_group(server):
    """A member added outside SCIM changes the hash of the group."""
    group = server.create_group("Admins")
    user = server.create_user(userName="bjensen")

    AuthUser.objects.get().groups.add(AuthGroup.objects.get())

    assert server.storage.get(server.groups, group.id).meta.version != (
        group.meta.version
    )
    assert server.storage.get(server.users, user.id).groups


def test_a_filter_finds_a_user_by_its_user_name(server):
    """Identity providers find the users they provision by userName."""
    server.create_user(userName="bjensen")
    server.create_user(userName="jsmith")

    page = server.storage.search(
        [server.users], SearchRequest(filter='userName eq "BJensen"')
    )

    assert [r.user_name for r in page.resources] == ["bjensen"]


def test_the_schemas_hold_the_fields_of_the_user_model():
    (schema,) = user_mapping().schemas()

    assert {attribute.name for attribute in schema.attributes} == {
        "userName",
        "name",
        "emails",
        "active",
        "password",
        "groups",
    }


def test_the_mapping_takes_the_attributes_of_the_project():
    users = user_mapping(
        {
            "active": None,
            "title": "last_name",
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:employeeNumber": "first_name",
        },
        model=User[EnterpriseUser],
    )
    groups = group_mapping({"members": None})

    (schema, extension) = users.schemas()

    assert "active" not in {attribute.name for attribute in schema.attributes}
    assert "title" in {attribute.name for attribute in schema.attributes}
    assert [a.name for a in extension.attributes] == ["employeeNumber"]
    assert [a.name for a in groups.schemas()[0].attributes] == ["displayName"]


def test_a_user_model_gives_the_attributes_it_has_fields_for(settings):
    """A member has no names, its user name is its email, and its groups are not those of django.contrib.auth."""
    settings.AUTH_USER_MODEL = "tests.Member"
    server = Server()

    user = server.create_user(userName="bjensen@example.com", password="secret")

    (schema,) = server.storage.mappings["User"].schemas()
    record = Member.objects.get()
    assert {attribute.name for attribute in schema.attributes} == {
        "userName",
        "password",
    }
    assert user.id == f"u{record.pk}"
    assert server.storage.get(server.users, user.id).user_name == record.email
    assert [
        a.name for a in server.storage.mappings["Group"].schemas()[0].attributes
    ] == ["displayName"]


def test_an_empty_prefix_makes_the_primary_key_the_id(settings):
    settings.AUTH_USER_MODEL = "tests.Member"
    server = Server(users=user_mapping(prefix=""))

    user = server.create_user(userName="bjensen@example.com")

    assert user.id == str(Member.objects.get().pk)


def test_a_password_is_write_only():
    (schema,) = user_mapping().schemas()
    (password,) = [a for a in schema.attributes if a.name == "password"]

    assert password.mutability == Mutability.write_only


def test_a_password_can_be_made_unusable_whatever_the_client_sends():
    """A project whose users log in through the identity provider only ignores the passwords."""
    unusable = Transformed("password", to_field=lambda value: make_password(None))
    server = Server(users=user_mapping({"password": unusable}))

    server.create_user(userName="bjensen", password="t1meMa$heen")

    assert not AuthUser.objects.get().has_usable_password()


@pytest.mark.parametrize("password", ["", None])
def test_an_empty_password_is_unusable(server, password):
    """Django hashes an empty password into one anyone could log in with."""
    server.create_user(userName="bjensen", password=password)

    record = AuthUser.objects.get()
    assert not record.has_usable_password()
    assert not record.check_password("")


def test_a_password_follows_the_validators_of_the_project(server, settings):
    settings.AUTH_PASSWORD_VALIDATORS = [
        {
            "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
            "OPTIONS": {"min_length": 12},
        }
    ]

    with pytest.raises(InvalidValueException):
        server.create_user(userName="bjensen", password="short")

    server.create_user(userName="bjensen", password="long enough to pass")
    assert AuthUser.objects.get().check_password("long enough to pass")


def test_a_new_password_changes_the_version(server):
    """The hash of the user covers the hash of its password, which it never returns."""
    user = server.create_user(userName="bjensen", password="t1meMa$heen")
    user.password = "an0therOne!"

    updated = server.storage.update(server.users, user)

    assert updated.meta.version != user.meta.version


def test_an_id_of_another_type_finds_nothing(server):
    group = server.create_group("Admins")
    server.create_user(userName="bjensen")

    with pytest.raises(NotFoundException):
        server.storage.get(server.users, group.id)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"name": None}, set()),
        ({"name.givenname": "last_name"}, {"givenName", "familyName"}),
    ],
    ids=["holding attribute", "other case"],
)
def test_a_path_of_the_project_replaces_the_attributes_it_overlaps(changes, expected):
    (schema,) = user_mapping(changes).schemas()
    names = [a for a in schema.attributes if a.name == "name"]

    assert {s.name for name in names for s in name.sub_attributes} == expected


def test_groups_without_a_relation_back_have_no_members(settings):
    """A guest knows its groups, but a group cannot list its guests."""
    settings.AUTH_USER_MODEL = "tests.Guest"
    server = Server()

    (users,) = server.storage.mappings["User"].schemas()
    (groups,) = server.storage.mappings["Group"].schemas()

    assert "groups" in {attribute.name for attribute in users.attributes}
    assert [attribute.name for attribute in groups.attributes] == ["displayName"]
