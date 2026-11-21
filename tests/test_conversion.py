import base64
import datetime

import pytest
from django.db.models import TextField
from django.db.models import Value
from django.db.models.functions import Concat
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import InvalidValueException
from scim2_models import MutabilityException
from scim2_models import User

from scim2_django.storage import Attribute
from scim2_django.storage import Computed
from scim2_django.storage import Many
from scim2_django.storage import ResourceMapping
from scim2_django.storage.conversion import _as_scim
from scim2_django.storage.conversion import _check_storable
from scim2_django.storage.conversion import _comparable
from scim2_django.storage.conversion import _from_scim
from scim2_django.storage.conversion import _link_entries
from scim2_django.storage.conversion import _to_scim
from scim2_django.storage.conversion import _write_collections

from .mappings import ENTERPRISE
from .mappings import groups_mapping
from .mappings import users_mapping
from .models import CertificateRecord
from .models import GroupRecord
from .models import Person
from .models import TaggedUser
from .models import UserRecord
from .test_mapping import OWNED
from .test_mapping import REQUIRED
from .test_mapping import TAGGED
from .test_mapping import Owned
from .test_mapping import Tagged
from .test_mapping import tagged_mapping

NOW = datetime.datetime(2026, 11, 19, 8, 0, tzinfo=datetime.UTC)
ENDPOINTS = {"User": "Users", "Group": "Groups"}

pytestmark = pytest.mark.django_db


def bound(users=None, groups=None):
    """Return the mappings of the users and the groups, bound to each other."""
    users, groups = users or users_mapping(), groups or groups_mapping()
    mappings = {"User": users, "Group": groups}
    targets = [(mapping, mapping._targets(mappings)) for mapping in (users, groups)]
    for mapping, links in targets:
        mapping._bind(links)

    return users, groups


def collections(mapping):
    return list(mapping._all_collections())


def save(mapping, resource, record, links=None, creating=True):
    """Write a resource in a record in the two phases a storage follows, and reload the record."""
    links = links or {}
    for collection in collections(mapping):
        links.setdefault(collection, [])

    record.created = record.created or NOW
    record.last_modified = NOW
    _from_scim(mapping, resource, record, links, creating=creating)
    record.save()
    _write_collections(mapping, resource, record, links, creating=creating)
    return type(record).objects.get(pk=record.pk)


def to_scim(mapping, record, resource_type="User"):
    return _to_scim(
        mapping,
        record,
        resource_type,
        mapping.model,
        ENDPOINTS,
        collections(mapping),
    )


def test_a_user_is_written_and_read_back():
    """The fields, the entries and the links of a user go through the two phases."""
    users, groups = bound()
    manager = UserRecord.objects.create(
        user_name="boss", created=NOW, last_modified=NOW
    )
    team = GroupRecord.objects.create(
        display_name="Team", created=NOW, last_modified=NOW
    )
    team.members.add(manager)
    resource = users.model.model_validate(
        {
            "userName": "bjensen",
            "name": {"givenName": "Barbara", "familyName": "Jensen"},
            "active": True,
            "password": "secret",
            "emails": [
                {"value": "bjensen@example.com", "type": "work", "primary": True},
                {"value": "babs@example.com"},
            ],
            ENTERPRISE: {"employeeNumber": "42", "manager": {"value": str(manager.id)}},
        }
    )
    manager_link = next(c for c in collections(users) if c.single)
    record = save(users, resource, UserRecord(), {manager_link: [manager]})
    record.groups.add(team)

    user = to_scim(users, record)

    assert user.user_name == "bjensen"
    assert user.name.given_name == "Barbara"
    assert user.active is True
    assert user.password is None
    assert record.password == "secret"
    assert user.locale == "bjensen"
    assert [(e.value, e.type, e.primary) for e in user.emails] == [
        ("bjensen@example.com", "work", True),
        ("babs@example.com", None, None),
    ]
    assert [(g.value, g.display, g.type, g.ref) for g in user.groups] == [
        (str(team.id), "Team", "direct", f"Groups/{team.id}")
    ]
    assert user[EnterpriseUser].employee_number == "42"
    assert user[EnterpriseUser].manager.value == str(manager.id)
    assert user.id == str(record.id)
    assert user.meta.resource_type == "User"
    assert user.meta.created == NOW
    assert user.meta.version == 'W/"1"'


def test_writing_replaces_the_entries_and_the_links():
    """The former emails are deleted, and the members are the new ones."""
    users, groups = bound()
    alice = UserRecord.objects.create(user_name="alice", created=NOW, last_modified=NOW)
    bob = UserRecord.objects.create(user_name="bob", created=NOW, last_modified=NOW)
    EmailRecordModel = alice.emails.model
    EmailRecordModel.objects.create(user=alice, value="old@example.com")
    members = next(c for c in collections(groups) if c.resource_type == "User")
    group = save(
        groups,
        Group(display_name="Team", members=[{"value": str(alice.id)}]),
        GroupRecord(),
        {members: [alice]},
    )

    group = save(
        groups,
        Group(display_name="Team", members=[{"value": str(bob.id)}]),
        group,
        {members: [bob]},
        creating=False,
    )
    alice = save(
        users,
        users.model(user_name="alice", emails=[{"value": "new@example.com"}]),
        alice,
        creating=False,
    )

    assert list(group.members.all()) == [bob]
    assert [email.value for email in alice.emails.all()] == ["new@example.com"]


def test_a_group_reads_its_users_and_its_groups():
    """Each Link of the members gives its entries their type."""
    users, groups = bound()
    alice = UserRecord.objects.create(
        user_name="alice", display_name="Alice", created=NOW, last_modified=NOW
    )
    child = GroupRecord.objects.create(
        display_name="Child", created=NOW, last_modified=NOW
    )
    parent = GroupRecord.objects.create(
        display_name="Parent", created=NOW, last_modified=NOW
    )
    parent.members.add(alice)
    parent.subgroups.add(child)

    group = to_scim(groups, parent, "Group")

    assert [(m.value, m.display, m.type) for m in group.members] == [
        (str(alice.id), "Alice", "User"),
        (str(child.id), "Child", "Group"),
    ]


def test_a_removed_manager_clears_the_foreign_key():
    """A Link holding one entry is written before the record is saved."""
    users, _ = bound()
    boss = UserRecord.objects.create(user_name="boss", created=NOW, last_modified=NOW)
    record = UserRecord.objects.create(
        user_name="bjensen", manager=boss, created=NOW, last_modified=NOW
    )

    record = save(users, users.model(user_name="bjensen"), record, creating=False)

    assert record.manager is None
    assert to_scim(users, record)[EnterpriseUser] is None


def test_a_new_record_keeps_the_defaults_of_the_missing_values():
    """The user type has a default, which a missing value does not replace."""
    users, _ = bound()

    record = save(users, users.model(user_name="bjensen"), UserRecord())

    assert record.user_type == "Employee"


def test_an_update_clears_the_missing_values():
    """A replaced resource without a title has no title anymore."""
    users, _ = bound()
    record = UserRecord.objects.create(
        user_name="bjensen", title="Boss", created=NOW, last_modified=NOW
    )

    record = save(users, users.model(user_name="bjensen"), record, creating=False)

    assert record.title is None


def test_an_update_keeps_a_write_only_value_it_does_not_carry():
    """A resource never carries its password back, so its absence changes nothing."""
    users, _ = bound()
    record = UserRecord.objects.create(
        user_name="bjensen", password="secret", created=NOW, last_modified=NOW
    )

    record = save(users, users.model(user_name="bjensen"), record, creating=False)

    assert record.password == "secret"


def test_read_only_entries_cannot_change():
    """Emails whose value is only read cannot be replaced by other emails."""
    users, _ = bound(
        users_mapping(
            {"emails": Many("emails", {"value": Attribute("value", writable=False)})}
        )
    )
    stored = users.model(user_name="a", emails=[{"value": "kept@example.com"}])

    with pytest.raises(MutabilityException, match="read-only on this server"):
        _check_storable(
            users,
            users.model(user_name="a", emails=[{"value": "new@example.com"}]),
            stored,
        )


def test_a_list_of_strings_is_written_and_read_back():
    """The values of a multi-valued simple attribute are entries of one field."""
    mapping = tagged_mapping()
    mapping._bind(mapping._targets({}))
    resource = mapping.model.model_validate(
        {"userName": "bjensen", TAGGED: {"tags": ["a", "b"]}}
    )

    record = save(mapping, resource, TaggedUser())

    assert to_scim(mapping, record)[Tagged].tags == ["a", "b"]


def test_binary_values_are_read_in_base64():
    """A certificate is stored as bytes, and returned encoded."""
    users, _ = bound(
        users_mapping({"x509Certificates": Many("certificates", {"value": "value"})})
    )
    record = UserRecord.objects.create(
        user_name="bjensen", created=NOW, last_modified=NOW
    )
    CertificateRecord.objects.create(user=record, value=b"\x00\x01")

    user = to_scim(users, record)

    assert user.x509_certificates[0].value == b"\x00\x01"


def test_a_database_returning_a_memoryview_is_read_in_base64():
    """PostgreSQL returns binary values as memoryviews."""
    assert _as_scim(memoryview(b"\x00\x01")) == base64.b64encode(b"\x00\x01").decode()


def test_a_date_without_time_zone_is_taken_as_utc():
    """Django returns naive dates when USE_TZ is off."""
    assert _as_scim(datetime.datetime(2026, 1, 1)) == datetime.datetime(
        2026, 1, 1, tzinfo=datetime.UTC
    )


def test_a_computed_attribute_is_read_from_its_annotation():
    """The storage annotates the records with the expressions of the mapping."""
    users, _ = bound(
        users_mapping(
            {
                "displayName": Computed(
                    Concat(
                        "given_name",
                        Value(" "),
                        "family_name",
                        output_field=TextField(),
                    )
                )
            }
        )
    )
    UserRecord.objects.create(
        user_name="bjensen",
        given_name="Barbara",
        family_name="Jensen",
        created=NOW,
        last_modified=NOW,
    )
    record = UserRecord.objects.annotate(**users._annotations()).get()

    assert to_scim(users, record).display_name == "Barbara Jensen"


def test_an_attribute_the_mapping_does_not_store_is_refused():
    """The phone numbers would be lost, so the write is refused."""
    users, _ = bound()

    with pytest.raises(InvalidValueException, match="phoneNumbers"):
        _check_storable(
            users, users.model(user_name="a", phone_numbers=[{"value": "1"}]), None
        )


def test_a_read_only_attribute_cannot_change():
    """The locale is generated, so a client cannot set it."""
    users, _ = bound()

    with pytest.raises(MutabilityException, match="read-only on this server"):
        _check_storable(users, users.model(user_name="a", locale="fr"), None)


def test_a_value_equal_to_the_stored_one_is_accepted():
    """A resource read and sent back can be written."""
    users, _ = bound()
    stored = users.model(user_name="a", locale="a")

    _check_storable(users, users.model(user_name="a", locale="a"), stored)


def test_a_value_the_field_cannot_lose_is_kept():
    """Whether a person is enabled is a boolean without null, so removing it is refused."""
    mapping = ResourceMapping(
        User, Person, REQUIRED | {"active": "enabled"}, version="version"
    )
    mapping.schemas()

    with pytest.raises(MutabilityException, match="cannot be removed"):
        _check_storable(mapping, User(), User(active=True))


def test_a_value_outside_the_choices_is_refused():
    """The title of a person is one of the choices of its field."""
    mapping = ResourceMapping(
        User, Person, REQUIRED | {"title": "label"}, version="version"
    )
    mapping.schemas()

    _check_storable(mapping, User(title="Ms"), None)
    with pytest.raises(InvalidValueException, match="cannot hold 'Dr'"):
        _check_storable(mapping, User(title="Dr"), None)


def test_the_display_of_a_link_is_ignored():
    """A member display is read from the linked resource, so a client may send any."""
    _, groups = bound()

    _check_storable(groups, Group(members=[{"value": "1", "display": "Whoever"}]), None)


def test_a_link_without_value_is_refused():
    """A member with a $ref only cannot be stored."""
    _, groups = bound()

    with pytest.raises(InvalidValueException, match="entry without value"):
        _link_entries(groups, Group(members=[{"display": "x"}]))


def test_the_links_of_a_resource_are_listed_by_attribute():
    """Each written attribute linking to resources comes with its Links and its entries."""
    users, groups = bound()

    links = _link_entries(groups, Group(members=[{"value": "1"}, {"value": "2"}]))
    ((collections_, entries),) = links
    assert [c.resource_type for c in collections_] == ["User", "Group"]
    assert [entry.value for entry in entries] == ["1", "2"]
    user_links = _link_entries(
        users, users.model.model_validate({ENTERPRISE: {"manager": {"value": "3"}}})
    )
    assert [(c[0].resource_type, [e.value for e in e_]) for c, e_ in user_links] == [
        ("User", ["3"])
    ]


def test_missing_values_in_entries_are_no_value():
    """A path through entries holding none of a sub-attribute gives no value."""
    assert _comparable([None, None]) is None
    assert _comparable(["a", None]) == ["a", None]


def test_a_date_with_a_time_zone_is_compared_in_utc():
    """Two dates of the same instant compare equal, whatever their time zone."""
    paris = datetime.timezone(datetime.timedelta(hours=1))

    assert _comparable(
        datetime.datetime(2026, 1, 1, 9, tzinfo=paris)
    ) == datetime.datetime(2026, 1, 1, 8, tzinfo=datetime.UTC)


def test_values_of_a_list_only_read_are_not_written():
    """Read-only tags keep their former entries."""
    mapping = tagged_mapping(Attribute("value", writable=False))
    mapping._bind(mapping._targets({}))
    record = TaggedUser.objects.create(user_name="a", created=NOW, last_modified=NOW)
    record.tags.create(value="kept")
    resource = mapping.model.model_validate({"userName": "a", TAGGED: {"tags": ["x"]}})

    record = save(mapping, resource, record, creating=False)

    assert [tag.value for tag in record.tags.all()] == ["kept"]


def test_a_link_gets_no_ref_without_the_endpoint_of_its_resource_type():
    """Without a provider, the storage does not know the endpoints."""
    _, groups = bound()
    alice = UserRecord.objects.create(user_name="alice", created=NOW, last_modified=NOW)
    team = GroupRecord.objects.create(
        display_name="Team", created=NOW, last_modified=NOW
    )
    team.members.add(alice)

    group = _to_scim(groups, team, "Group", Group, {}, collections(groups))

    assert [(m.value, m.ref) for m in group.members] == [(str(alice.id), None)]


def test_entries_without_ordering_come_in_the_order_of_their_primary_key():
    """The certificates have no ordering, so a client reads them in a stable order."""
    users, _ = bound(
        users_mapping({"x509Certificates": Many("certificates", {"value": "value"})})
    )
    record = UserRecord.objects.create(user_name="a", created=NOW, last_modified=NOW)
    for value in (b"\x02", b"\x01", b"\x03"):
        CertificateRecord.objects.create(user=record, value=value)

    values = [c.value for c in to_scim(users, record).x509_certificates]

    assert values == [b"\x02", b"\x01", b"\x03"]


def test_an_entry_without_a_value_its_field_needs_is_refused():
    """A certificate value cannot be null, so a certificate without it would fail in the database."""
    users, _ = bound(
        users_mapping(
            {
                "x509Certificates": Many(
                    "certificates", {"value": "value", "display": "display"}
                )
            }
        )
    )
    _check_storable(
        users, users.model(user_name="a", x509_certificates=[{"value": "AAE="}]), None
    )

    with pytest.raises(
        InvalidValueException, match="x509Certificates.value' is required"
    ):
        _check_storable(
            users,
            users.model(user_name="a", x509_certificates=[{"display": "x"}]),
            None,
        )


def test_dates_are_written_without_time_zone_when_use_tz_is_off(settings):
    """Django refuses aware dates when USE_TZ is off, so they are written in UTC without zone."""
    settings.USE_TZ = False
    paris = datetime.timezone(datetime.timedelta(hours=1))

    assert _comparable(
        datetime.datetime(2026, 1, 1, 9, tzinfo=paris)
    ) == datetime.datetime(2026, 1, 1, 8)


def test_missing_values_of_a_list_are_left_out():
    """A tag whose field holds no value is not a tag."""
    mapping = tagged_mapping("note")
    mapping._bind(mapping._targets({}))
    record = TaggedUser.objects.create(user_name="a", created=NOW, last_modified=NOW)
    record.tags.create(value="x", note=None)
    record.tags.create(value="y", note="kept")

    assert to_scim(mapping, record)[Tagged].tags == ["kept"]


def test_a_new_resource_needs_the_values_its_fields_cannot_do_without():
    """Whether a person is enabled has neither null nor a default, so creating one without it is refused."""
    mapping = ResourceMapping(
        User[Owned],
        Person,
        REQUIRED
        | {
            "active": "enabled",
            "title": "status",
            f"{OWNED}:since": "touched",
            f"{OWNED}:verified": "verified",
        },
        version="version",
    )
    mapping.schemas()

    _check_storable(mapping, mapping.model(active=True), None)
    with pytest.raises(InvalidValueException, match=":active' is required"):
        _check_storable(mapping, mapping.model(title="Ms"), None)


def test_a_converting_field_is_read_through_its_conversion():
    """The storage returns what the field reads, the conversion included."""
    mapping = ResourceMapping(
        User,
        Person,
        REQUIRED | {"userName": "user_name", "title": "code"},
        version="version",
    )
    mapping.schemas()
    mentor = UserRecord.objects.create(user_name="m", created=NOW, last_modified=NOW)
    Person.objects.create(
        id="p",
        user_name="a",
        code="ceo",
        enabled=True,
        mentor=mentor,
        created=NOW,
        last_modified=NOW,
    )

    assert to_scim(mapping, Person.objects.get()).title == "CEO"


def test_an_entry_may_leave_out_a_value_its_field_has_a_default_for():
    """A certificate is not primary unless it says so."""
    users, _ = bound(
        users_mapping(
            {
                "x509Certificates": Many(
                    "certificates", {"value": "value", "primary": "primary"}
                )
            }
        )
    )

    _check_storable(
        users, users.model(user_name="a", x509_certificates=[{"value": "AAE="}]), None
    )


def test_an_empty_string_is_no_value():
    """A text field without null stores an empty string for a missing value, and reads it as none."""
    mapping = ResourceMapping(
        User, Person, REQUIRED | {"userName": "user_name"}, version="version"
    )
    mapping.schemas()
    mentor = UserRecord.objects.create(user_name="m", created=NOW, last_modified=NOW)
    person = Person(id="p", user_name="a", mentor=mentor, enabled=True)

    _check_storable(mapping, User(), User(user_name="a"))
    person = save(mapping, User(), person, creating=False)

    assert person.user_name == ""
    assert to_scim(mapping, person).user_name is None
