import datetime
import json

import pytest
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import User as AuthUser
from django.contrib.auth.models import update_last_login
from django.core import serializers
from django.core.exceptions import ImproperlyConfigured
from django.db.models.signals import m2m_changed
from scim2_models import SearchRequest

from examples.models import AccountScim
from examples.models import TeamScim
from scim2_django.auth import user_mapping
from scim2_django.auth.signals import _date
from scim2_django.auth.signals import connect

from .models import Guest
from .models import Member
from .models import Team
from .test_auth import Server

pytestmark = pytest.mark.django_db

LONG_AGO = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)


@pytest.fixture
def server():
    return Server()


def test_the_model_beside_the_users_holds_their_external_id(server):
    user = server.create_user(userName="bjensen", externalId="42")

    scim = AccountScim.objects.get()
    assert scim.external_id == user.external_id == "42"
    assert user.meta.last_modified == scim.last_modified


def test_a_group_created_through_scim_has_a_creation_date(server):
    group = server.create_group("Admins")

    scim = TeamScim.objects.get()
    assert group.meta.created == scim.created
    assert group.meta.last_modified == scim.last_modified


def test_a_group_created_outside_scim_has_no_creation_date(server):
    """Its record beside it holds only the date of its last change."""
    AuthGroup.objects.create(name="Admins")
    group = server.storage.search([server.groups], SearchRequest()).resources[0]

    updated = server.storage.update(server.groups, group)

    assert TeamScim.objects.get().created is None
    assert updated.meta.created is None
    assert updated.meta.last_modified == TeamScim.objects.get().last_modified


def test_a_user_saved_outside_scim_is_dated():
    """The users a project creates or changes itself get a modification date too."""
    user = AuthUser.objects.create(username="bjensen")
    AccountScim.objects.filter(pk=user.pk).update(last_modified=LONG_AGO)

    user.first_name = "Barbara"
    user.save()

    assert AccountScim.objects.get().last_modified > LONG_AGO


def test_a_login_keeps_the_modification_date():
    """A login changes nothing a user holds in SCIM."""
    user = AuthUser.objects.create(username="bjensen")
    AccountScim.objects.filter(pk=user.pk).update(last_modified=LONG_AGO)

    update_last_login(None, user)

    assert AccountScim.objects.get().last_modified == LONG_AGO


def test_a_loaded_fixture_keeps_its_dates():
    """Loaddata saves the records as they are, the records beside them included."""
    data = json.dumps(
        [
            {
                "model": "auth.user",
                "pk": 5,
                "fields": {"username": "bjensen", "password": ""},
            }
        ]
    )

    for record in serializers.deserialize("json", data):
        record.save()

    assert AuthUser.objects.exists()
    assert not AccountScim.objects.exists()


@pytest.mark.parametrize("side", ["user", "group"])
@pytest.mark.parametrize("change", ["add", "remove", "clear"])
def test_a_change_of_the_members_dates_the_user_and_the_group(side, change):
    """A group lists its members, and a user its groups, so both change."""
    user = AuthUser.objects.create(username="bjensen")
    group = AuthGroup.objects.create(name="Admins")
    if change != "add":
        user.groups.add(group)

    AccountScim.objects.update(last_modified=LONG_AGO)
    TeamScim.objects.update(last_modified=LONG_AGO)
    links = user.groups if side == "user" else group.user_set
    other = group if side == "user" else user

    if change == "clear":
        links.clear()
    else:
        getattr(links, change)(other)

    assert AccountScim.objects.get().last_modified > LONG_AGO
    assert TeamScim.objects.get().last_modified > LONG_AGO


def test_a_scim_write_on_a_user_dated_by_a_handler_stores_its_values(server):
    """The handler creates the record beside the user while the storage writes it."""
    record = AuthUser.objects.create(username="bjensen")
    AccountScim.objects.all().delete()
    user = server.storage.get(server.users, f"u{record.pk}")
    user.external_id = "42"

    server.storage.update(server.users, user)

    assert AccountScim.objects.get().external_id == "42"


def test_a_model_beside_the_users_maps_the_fields_it_keeps(settings):
    """The model beside the guests has no modification date."""
    settings.AUTH_USER_MODEL = "tests.Guest"

    users = user_mapping()
    users.check()

    assert "scim__external_id" in {column.key for column in users._columns.values()}
    assert users._last_modified is None


def test_several_models_beside_the_users_are_refused(settings):
    settings.AUTH_USER_MODEL = "tests.Twin"

    with pytest.raises(ImproperlyConfigured, match="LeftTwinScim, tests.RightTwinScim"):
        user_mapping()


def test_a_user_model_without_groups_gets_no_handler_of_its_links(settings):
    """The groups of a member are not those of django.contrib.auth."""
    settings.AUTH_USER_MODEL = "tests.Member"

    connect()

    assert not m2m_changed.has_listeners(Member.groups.through)


def test_a_model_without_modification_date_is_not_dated(settings):
    settings.AUTH_USER_MODEL = "tests.Guest"
    guest = Guest.objects.create(name="bjensen")

    _date(Guest, {guest.pk}, "default")

    assert not guest.__class__.objects.filter(scim__isnull=False).exists()


def test_users_without_model_beside_them_are_not_dated(
    settings, django_assert_num_queries
):
    settings.AUTH_USER_MODEL = "tests.Member"
    member = Member.objects.create(email="bjensen@example.com")

    with django_assert_num_queries(0):
        _date(Member, {member.pk}, "default")


def test_records_of_other_models_are_not_dated(django_assert_num_queries):
    with django_assert_num_queries(1):
        Member.objects.create(email="bjensen@example.com")


@pytest.mark.parametrize("side", ["user", "group"])
def test_a_removal_dates_only_the_records_it_unlinks(side):
    """A group the user is not in, and one that does not exist, are left as they are."""
    user = AuthUser.objects.create(username="bjensen")
    linked = AuthGroup.objects.create(name="Admins")
    other = AuthGroup.objects.create(name="Staff")
    user.groups.add(linked)
    AccountScim.objects.update(last_modified=LONG_AGO)
    TeamScim.objects.update(last_modified=LONG_AGO)

    if side == "user":
        user.groups.remove(linked, other, 999)
    else:
        other.user_set.remove(user)

    dates = dict(TeamScim.objects.values_list("group__name", "last_modified"))
    assert dates["Staff"] == LONG_AGO
    assert (dates["Admins"] > LONG_AGO) == (side == "user")
    assert not TeamScim.objects.filter(pk=999).exists()


def test_a_group_changed_through_a_proxy_model_is_dated():
    team = Team.objects.create(name="Admins")
    TeamScim.objects.update(last_modified=LONG_AGO)

    team.name = "Staff"
    team.save()
    after_save = TeamScim.objects.get().last_modified
    TeamScim.objects.update(last_modified=LONG_AGO)
    team.user_set.add(AuthUser.objects.create(username="bjensen"))

    assert after_save > LONG_AGO
    assert TeamScim.objects.get().last_modified > LONG_AGO


def test_a_new_group_with_members_is_modified_when_created(server):
    """The members set after the group do not date it again."""
    user = server.create_user(userName="bjensen")

    group = server.create_group("Admins", [user.id])

    assert group.meta.last_modified == group.meta.created


@pytest.mark.django_db(databases=["default", "other"])
def test_a_clear_dates_the_records_of_its_database():
    user = AuthUser.objects.using("other").create(username="bjensen")
    group = AuthGroup.objects.using("other").create(name="Admins")
    user.groups.add(group)
    AccountScim.objects.using("other").update(last_modified=LONG_AGO)
    TeamScim.objects.using("other").update(last_modified=LONG_AGO)

    user.groups.clear()

    assert TeamScim.objects.using("other").get().last_modified > LONG_AGO
    assert AccountScim.objects.using("other").get().last_modified > LONG_AGO


def test_a_change_linking_nothing_dates_nothing():
    """The user is already in the group, and the other group has no member."""
    user = AuthUser.objects.create(username="bjensen")
    group = AuthGroup.objects.create(name="Admins")
    empty = AuthGroup.objects.create(name="Staff")
    user.groups.add(group)
    AccountScim.objects.update(last_modified=LONG_AGO)
    TeamScim.objects.update(last_modified=LONG_AGO)

    user.groups.add(group)
    empty.user_set.clear()

    assert set(AccountScim.objects.values_list("last_modified", flat=True)) == {
        LONG_AGO
    }
    assert set(TeamScim.objects.values_list("last_modified", flat=True)) == {LONG_AGO}
