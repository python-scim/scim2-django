import sys
from typing import Annotated

import pytest
from django.apps import apps
from django.core.checks import registry
from django.core.exceptions import AppRegistryNotReady
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.db.models import Value
from django.db.models.functions import Concat
from scim2_models import URN
from scim2_models import ComplexAttribute
from scim2_models import Extension
from scim2_models import Group
from scim2_models import Mutability
from scim2_models import Required
from scim2_models import User

from scim2_django.storage import Attribute
from scim2_django.storage import Computed
from scim2_django.storage import Link
from scim2_django.storage import Many
from scim2_django.storage import ResourceMapping
from scim2_django.storage.mapping import check_mappings

from .mappings import ENTERPRISE
from .mappings import groups_mapping
from .mappings import users_mapping
from .models import GroupRecord
from .models import Person
from .models import TaggedUser
from .models import UserRecord

TAGGED = "urn:example:params:scim:schemas:extension:tagged:2.0:User"


class Badge(ComplexAttribute):
    lines: list[str] | None = None


class Owner(ComplexAttribute):
    value: str | None = None
    display: Annotated[str | None, Mutability.read_only] = None


class Owned(Extension):
    __schema__ = URN("urn:example:params:scim:schemas:extension:owned:2.0:User")

    owner: Annotated[Owner | None, Required.true] = None
    sponsor: Owner | None = None


OWNED = "urn:example:params:scim:schemas:extension:owned:2.0:User"


class Tagged(Extension):
    __schema__ = URN(TAGGED)

    tags: list[str] | None = None
    badge: Badge | None = None


REQUIRED = {
    "id": "id",
    "meta.created": "created",
    "meta.lastModified": "last_modified",
}


def person_mapping(version="version", **attributes):
    return ResourceMapping(User, Person, REQUIRED | attributes, version=version)


def tagged_mapping(tags="value"):
    return ResourceMapping(
        User[Tagged],
        TaggedUser,
        REQUIRED | {"userName": "user_name", f"{TAGGED}:tags": Many("tags", tags)},
        version="version",
    )


def error_ids(mapping):
    return [message.id for message in mapping.check()]


@pytest.mark.parametrize("missing", ["id", "meta.created", "meta.lastModified"])
def test_a_mapping_needs_the_identifier_and_the_dates(missing):
    """The storage fills these attributes, so it needs a field for each."""
    attributes = {key: value for key, value in REQUIRED.items() if key != missing}

    with pytest.raises(ValueError, match=missing):
        ResourceMapping(User, Person, attributes, version="version")


def test_an_unknown_path_is_refused():
    """A typo in a path shows up when the mapping is created."""
    with pytest.raises(ValueError, match="'usrName' is not an attribute of User"):
        person_mapping(usrName="user_name")


def test_a_complex_attribute_is_mapped_by_its_sub_attributes():
    """A field holds one value, not a whole name."""
    with pytest.raises(ValueError, match="map its sub-attributes"):
        person_mapping(name="user_name")


def test_a_multi_valued_attribute_is_not_mapped_to_a_field():
    """A field holds one value, not a list of emails."""
    with pytest.raises(ValueError, match="map it with Many"):
        person_mapping(emails="user_name")


def test_many_maps_a_multi_valued_complex_attribute_only():
    """Many describes entries, which userName has none of."""
    with pytest.raises(ValueError, match="not a multi-valued complex attribute"):
        person_mapping(userName=Many("addresses", {"value": "value"}))


def test_many_maps_a_multi_valued_simple_attribute_to_one_field():
    """A list of strings is held by one field of the entries, not by sub-attributes."""
    with pytest.raises(ValueError, match="not a multi-valued simple attribute"):
        person_mapping(emails=Many("addresses", "value"))


def test_link_maps_a_complex_attribute_only():
    """A Link gives a value and a display, which userName has no room for."""
    with pytest.raises(ValueError, match="not a complex attribute"):
        person_mapping(userName=Link("addresses", "User"))


def test_an_attribute_is_mapped_to_the_name_of_a_field():
    """A field object, or any other value, cannot be resolved later."""
    with pytest.raises(ValueError, match="must be mapped to the name of a field"):
        person_mapping(userName=Person._meta.get_field("user_name"))


def test_a_sub_attribute_holding_several_values_is_refused():
    """A field cannot hold the lines of a badge."""
    with pytest.raises(ValueError, match="holds several values in a complex attribute"):
        ResourceMapping(
            User[Tagged],
            TaggedUser,
            REQUIRED | {f"{TAGGED}:badge.lines": "user_name"},
            version="version",
        )


@pytest.mark.parametrize(
    "links, message",
    [
        ([], "at least one"),
        (
            [Link("members", "User"), Link("subgroups", "User")],
            "one Link per resource type",
        ),
        (
            [Link("members", "User", type="User"), Link("subgroups", "Group")],
            "every Link of 'members' has a type, or none",
        ),
        ([Link("members", "User"), "User"], "map it with Many or Link"),
    ],
    ids=["empty", "same resource type", "type on some", "not a Link"],
)
def test_the_links_of_an_attribute_are_checked(links, message):
    """The Links of an attribute link to distinct resource types, and give a type all or none."""
    with pytest.raises(ValueError, match=message):
        groups_mapping({"members": links})


def test_a_link_type_needs_a_type_sub_attribute():
    """The manager has no type sub-attribute, so its Link cannot give one."""
    with pytest.raises(ValueError, match="has no type sub-attribute"):
        users_mapping({f"{ENTERPRISE}:manager": Link("manager", "User", type="User")})


def test_valid_mappings_pass_the_checks():
    """The mappings of the users and the groups fit their fields."""
    assert users_mapping().check() == []
    assert groups_mapping().check() == []
    assert tagged_mapping().check() == []


def test_the_checks_run_once():
    """The fields of a mapping are resolved once, and the result is kept."""
    mapping = person_mapping(userName="usr_name")

    assert mapping.check() is mapping.check()


def test_an_unknown_field_is_reported():
    """A typo in a field name shows up in manage.py check."""
    mapping = person_mapping(userName="usr_name")

    (error,) = mapping.check()
    assert error.id == "scim2_django.E001"
    assert error.msg == (
        "'userName' is mapped to 'usr_name', which is not a field of tests.Person"
    )
    assert error.obj is mapping


def test_an_unknown_entry_field_is_reported():
    """The fields of the entries are looked up in the model of the entries."""
    mapping = person_mapping(emails=Many("addresses", {"value": "address"}))

    assert error_ids(mapping) == ["scim2_django.E001"]


def test_an_unknown_value_field_of_a_simple_attribute_is_reported():
    """The field holding the values of a list of strings is looked up in the model of the entries."""
    assert error_ids(tagged_mapping("label")) == ["scim2_django.E001"]


def test_an_unknown_relation_is_reported():
    """A Many and a Link name a relation of the model."""
    mapping = person_mapping(
        emails=Many("mailboxes", {"value": "value"}),
        groups=Link("teams", "Group"),
    )

    assert error_ids(mapping) == ["scim2_django.E001", "scim2_django.E001"]


def test_a_relation_is_not_mapped_as_a_field():
    """A relation holds records, not a value."""
    assert error_ids(users_mapping({"title": "manager"})) == ["scim2_django.E002"]


@pytest.mark.parametrize("relation", ["user_name", "manager", "groups"])
def test_many_needs_a_reverse_foreign_key(relation):
    """The entries hold a foreign key to their resource."""
    mapping = users_mapping({"emails": Many(relation, {"value": "value"})})

    assert error_ids(mapping) == ["scim2_django.E003"]


def test_many_needs_a_cascading_foreign_key():
    """Without it, deleting a resource would leave its entries behind."""
    mapping = person_mapping(emails=Many("notes", {"value": "value"}))

    (error,) = mapping.check()
    assert error.id == "scim2_django.E003"
    assert "on_delete=CASCADE" in error.msg


def test_a_single_link_needs_a_foreign_key():
    """The manager is one user, so a relation holding several records does not fit it."""
    mapping = users_mapping({f"{ENTERPRISE}:manager": Link("groups", "Group")})

    (error,) = mapping.check()
    assert error.id == "scim2_django.E003"
    assert "needs a foreign key or a one-to-one field" in error.msg


def test_a_single_link_needs_a_forward_relation():
    """A reverse relation is held by the other records, so writing the link would change them."""
    mapping = users_mapping({f"{ENTERPRISE}:manager": Link("reports", "User")})

    assert error_ids(mapping) == ["scim2_django.E003"]


def test_several_links_need_a_many_to_many_relation():
    """The groups of a user are several, so a foreign key holding one record does not fit them."""
    mapping = users_mapping({"groups": Link("manager", "Group")})

    (error,) = mapping.check()
    assert error.id == "scim2_django.E003"
    assert "needs a many-to-many relation" in error.msg


def test_the_version_is_an_integer_field():
    """A text field cannot count the writes."""
    mapping = person_mapping(version="label")

    assert error_ids(mapping) == ["scim2_django.E004"]


def test_the_version_is_a_field_of_the_record():
    """A version that is not a field shows up in manage.py check."""
    mapping = person_mapping(version="revision")

    assert error_ids(mapping) == ["scim2_django.E001"]


def test_the_identifier_is_unique():
    """Two resources sharing an id could not be told apart."""
    mapping = ResourceMapping(
        User, Person, REQUIRED | {"id": "user_name"}, version="version"
    )

    assert error_ids(mapping) == ["scim2_django.E005"]


def test_a_missing_identifier_field_is_reported_once():
    """Without a field for the identifier, its uniqueness is not checked."""
    mapping = ResourceMapping(
        User, Person, REQUIRED | {"id": "ident"}, version="version"
    )

    assert error_ids(mapping) == ["scim2_django.E001"]


def test_a_converting_field_holding_a_string_is_reported():
    """Filters and sorts compare the stored values, which such a field converts when it reads them."""
    mapping = person_mapping(title="data")

    (warning,) = mapping.check()
    assert warning.id == "scim2_django.W001"
    assert not warning.is_serious()


def test_a_converting_field_never_read_is_not_reported():
    """A write-only attribute is never filtered nor sorted."""
    mapping = person_mapping(title=Attribute("data", readable=False))

    assert mapping.check() == []


def test_a_mapping_with_errors_cannot_be_used():
    """A storage refuses a mapping that manage.py check would report."""
    mapping = person_mapping(userName="usr_name")

    with pytest.raises(ImproperlyConfigured, match="'usr_name', which is not a field"):
        mapping.schemas()


def test_a_mapping_with_warnings_can_be_used():
    """A warning tells about a doubtful mapping, without refusing it."""
    mapping = person_mapping(title="data")

    assert mapping.schemas()


@pytest.mark.django_db
def test_manage_py_check_reports_the_mappings():
    """The system check reports the mappings that do not fit their fields."""
    mapping = person_mapping(userName="usr_name")

    with pytest.raises(SystemCheckError, match=r"tests.Person>: \(scim2_django.E001\)"):
        call_command("check")
    assert mapping


@pytest.mark.django_db
def test_manage_py_check_is_silent_on_valid_mappings():
    """The valid mappings add nothing to the output of manage.py check."""
    mapping = users_mapping()

    call_command("check")
    assert mapping


def test_the_check_loads_the_urlconf_first(settings, monkeypatch):
    """The mappings are usually created by the URLconf, through the storages it serves."""
    monkeypatch.delitem(sys.modules, "tests.urls_with_mappings", raising=False)
    settings.ROOT_URLCONF = "tests.urls_with_mappings"

    messages = check_mappings()

    assert [message.id for message in messages] == ["scim2_django.E001"]


def test_the_check_leaves_a_broken_urlconf_to_django(settings):
    """The URL checks of Django report a URLconf that cannot be imported."""
    settings.ROOT_URLCONF = "tests.missing_urlconf"

    assert check_mappings() == []


def test_the_schemas_hold_the_mapped_attributes_only():
    """An attribute the mapping does not store is not published."""
    (user, enterprise) = users_mapping().schemas()

    names = {attribute.name for attribute in user.attributes}
    assert {"userName", "emails", "password"} <= names
    assert names.isdisjoint({"phoneNumbers", "profileUrl", "x509Certificates"})
    (name,) = [a for a in user.attributes if a.name == "name"]
    assert [sub.name for sub in name.sub_attributes] == ["familyName", "givenName"]
    assert [a.name for a in enterprise.attributes] == ["employeeNumber", "manager"]


def test_a_generated_field_is_published_read_only():
    """Locale comes from a generated field, which the storage cannot write."""
    (user, _) = users_mapping().schemas()

    (locale,) = [a for a in user.attributes if a.name == "locale"]
    assert locale.mutability == Mutability.read_only


def test_a_computed_attribute_is_published_read_only():
    """A query expression is read, never written."""
    computed = Computed(Concat("given_name", Value(" "), "family_name"))
    (user, _) = users_mapping({"displayName": computed}).schemas()

    (display_name,) = [a for a in user.attributes if a.name == "displayName"]
    assert display_name.mutability == Mutability.read_only


def test_a_field_only_read_is_published_read_only():
    """An Attribute that is not writable is published as such."""
    (user, _) = users_mapping({"title": Attribute("title", writable=False)}).schemas()

    (title,) = [a for a in user.attributes if a.name == "title"]
    assert title.mutability == Mutability.read_only


def test_a_complex_attribute_only_read_is_published_read_only():
    """Entries whose every mapped sub-attribute is only read cannot be written."""
    emails = Many("emails", {"value": Attribute("value", writable=False)})
    (user, _) = users_mapping({"emails": emails}).schemas()

    (emails,) = [a for a in user.attributes if a.name == "emails"]
    assert emails.mutability == Mutability.read_only


def test_a_link_publishes_its_value_its_ref_its_type_and_a_read_only_display():
    """A member is published before any storage links the mapping, with its $ref to the resource types of its Links."""
    (group,) = groups_mapping().schemas()

    (members,) = [a for a in group.attributes if a.name == "members"]
    assert [(sub.name, sub.mutability) for sub in members.sub_attributes] == [
        ("value", Mutability.immutable),
        ("$ref", Mutability.immutable),
        ("type", Mutability.immutable),
        ("display", Mutability.read_only),
    ]
    (ref,) = [sub for sub in members.sub_attributes if sub.name == "$ref"]
    assert ref.reference_types == ["User", "Group"]


def test_a_link_without_type_does_not_publish_the_type():
    """The type of a member is published only when its Links give one."""
    (group,) = groups_mapping({"members": Link("members", "User")}).schemas()

    (members,) = [a for a in group.attributes if a.name == "members"]
    assert "type" not in [sub.name for sub in members.sub_attributes]
    (ref,) = [sub for sub in members.sub_attributes if sub.name == "$ref"]
    assert ref.reference_types == ["User"]


def test_a_single_link_publishes_its_value_its_ref_and_a_read_only_display_name():
    """The manager is published with the sub-attributes the storage reads."""
    (_, enterprise) = users_mapping().schemas()

    (manager,) = [a for a in enterprise.attributes if a.name == "manager"]
    assert [(sub.name, sub.mutability) for sub in manager.sub_attributes] == [
        ("value", Mutability.read_write),
        ("$ref", Mutability.read_write),
        ("displayName", Mutability.read_only),
    ]


def test_a_list_of_strings_is_published():
    """The values of a multi-valued simple attribute are held by one field of the entries."""
    (_, tagged) = tagged_mapping().schemas()

    assert [a.name for a in tagged.attributes] == ["tags"]


def test_an_identifier_that_is_not_text_is_compared_as_text():
    """Filters compare a UUID or an integer identifier as text, and a text identifier as it is."""
    users, groups, people = users_mapping(), groups_mapping(), person_mapping()
    for mapping in (users, groups, people):
        mapping.schemas()

    assert users._column("id").textual
    assert groups._column("id").textual
    assert not people._column("id").textual


def test_links_need_the_mapping_of_their_resource_type():
    """A link to a resource type the storage does not serve cannot be read."""
    groups = groups_mapping({"members": Link("members", "User")})
    groups.schemas()

    with pytest.raises(
        ImproperlyConfigured, match="No mapping for the resource type 'User'"
    ):
        groups._bind(groups._targets({"Group": groups}))


def test_a_link_holds_the_records_of_the_mapping_it_links_to():
    """The members are users, so a mapping of groups cannot serve as their resource type."""
    groups = groups_mapping({"members": Link("members", "User")})
    groups.schemas()

    with pytest.raises(ImproperlyConfigured, match="links to tests.UserRecord records"):
        groups._bind(groups._targets({"User": groups}))


def test_a_mapping_links_to_the_same_mappings_in_every_storage():
    """A mapping shared by two storages cannot link to two different mappings."""
    groups = groups_mapping()
    for mapping in (groups, users := users_mapping(), other := users_mapping()):
        mapping.schemas()
    groups._bind(groups._targets({"User": users, "Group": groups}))

    with pytest.raises(ImproperlyConfigured, match="already links to another mapping"):
        groups._bind(groups._targets({"User": other, "Group": groups}))


def test_a_link_displays_the_user_name_without_a_display_name():
    """A user mapping without displayName still gives its members a display."""
    users = users_mapping({"displayName": None})
    users.schemas()

    assert users._display().key == "user_name"


def test_a_link_displays_nothing_without_a_name():
    """A group mapping without displayName gives its links no display."""
    groups = groups_mapping({"displayName": None})
    groups.schemas()

    assert groups._display() is None


def test_a_group_links_to_users_and_groups():
    """Each Link of the members gets the identifier and the display of its target."""
    users, groups = users_mapping(), groups_mapping()
    for mapping in (users, groups):
        mapping.schemas()
    for mapping in (users, groups):
        mapping._bind(mapping._targets({"User": users, "Group": groups}))

    user_link, group_link = groups._collections[(Group, "members")]
    assert user_link.target is users
    assert user_link.columns["value"].key == "id"
    assert user_link.columns["display"].key == "display_name"
    assert group_link.target is groups
    assert (user_link.entry_model, group_link.entry_model) == (UserRecord, GroupRecord)


def test_a_link_to_resources_without_a_name_has_no_display():
    """A member group without displayName is linked by its identifier only."""
    groups = groups_mapping({"displayName": None})
    users = users_mapping()
    for mapping in (users, groups):
        mapping.schemas()
    groups._bind(groups._targets({"User": users, "Group": groups}))

    _, group_link = groups._collections[(Group, "members")]
    assert list(group_link.columns) == ["value"]


def test_paths_ignore_the_case():
    """Per RFC 7643 §2.1, attribute names are case-insensitive."""
    mapping = ResourceMapping(
        User,
        Person,
        {"ID": "id", "Meta.Created": "created", "meta.LASTMODIFIED": "last_modified"},
        version="version",
    )

    assert mapping.check() == []


def test_two_paths_to_the_same_attribute_are_refused():
    """The second path would silently replace the first."""
    with pytest.raises(ValueError, match="'userName' and 'username' map the same"):
        users_mapping({"username": "title"})


@pytest.mark.parametrize(
    "value",
    [
        Computed(Value("2026-01-01")),
        Attribute("created", readable=False),
    ],
    ids=["computed", "write-only"],
)
def test_the_dates_are_mapped_to_the_name_of_a_field(value):
    """The storage writes the dates and reads them back."""
    with pytest.raises(ValueError, match="'meta.created' must be mapped to the name"):
        ResourceMapping(
            User, Person, REQUIRED | {"meta.created": value}, version="version"
        )


def test_the_dates_are_mapped_to_date_and_time_fields():
    """A text field would compare the dates as strings."""
    mapping = ResourceMapping(
        User, Person, REQUIRED | {"meta.lastModified": "label"}, version="version"
    )

    assert error_ids(mapping) == ["scim2_django.E006"]


def test_a_single_valued_attribute_is_mapped_to_one_link():
    """The manager is one user, so it cannot be read from two relations."""
    with pytest.raises(ValueError, match="'.*:manager' holds one value"):
        users_mapping(
            {
                f"{ENTERPRISE}:manager": [
                    Link("manager", "User"),
                    Link("groups", "Group"),
                ]
            }
        )


def test_the_fields_of_the_entries_are_named():
    """A field object, or any other value, cannot be resolved later."""
    field = Person._meta.get_field("label")
    with pytest.raises(ValueError, match="'emails.value' must be mapped to the name"):
        person_mapping(emails=Many("addresses", {"value": field}))
    with pytest.raises(ValueError, match=f"'{TAGGED}:tags' must be mapped to the name"):
        tagged_mapping(field)


def test_a_link_that_can_be_removed_needs_a_nullable_foreign_key():
    """Removing the sponsor would set a foreign key that cannot be null."""
    mapping = ResourceMapping(
        User[Owned],
        Person,
        REQUIRED | {f"{OWNED}:sponsor": Link("mentor", "User")},
        version="version",
    )

    (error,) = mapping.check()
    assert error.id == "scim2_django.E003"
    assert "needs null=True" in error.msg


def test_a_required_link_takes_a_foreign_key_that_cannot_be_null():
    """The owner is always there, so its foreign key is never cleared."""
    mapping = ResourceMapping(
        User[Owned],
        Person,
        REQUIRED | {f"{OWNED}:owner": Link("mentor", "User")},
        version="version",
    )

    assert mapping.check() == []


def test_a_string_mapped_to_a_field_not_holding_text_is_reported():
    """A UUID field compares its stored form, which is not the text clients read."""
    (warning,) = users_mapping({"externalId": "id"}).check()

    assert warning.id == "scim2_django.W001"


def test_the_checks_wait_for_the_models(monkeypatch):
    """Before the models are loaded, reverse relations look missing, and such a result must not be kept."""
    mapping = users_mapping()
    monkeypatch.setattr(apps, "models_ready", False)

    with pytest.raises(AppRegistryNotReady):
        mapping.check()
    monkeypatch.undo()
    assert mapping.check() == []


def test_the_app_registers_the_check():
    """With scim2_django in INSTALLED_APPS, the check runs before the URLconf is imported."""
    assert check_mappings in registry.registry.get_checks()


def test_binding_resolves_the_target_mappings():
    """A storage binds its mappings without resolving them first."""
    users, groups = users_mapping(), groups_mapping()

    groups._bind(groups._targets({"User": users, "Group": groups}))

    user_link, _ = groups._collections[(Group, "members")]
    assert user_link.columns["value"].key == "id"


def test_a_failed_binding_binds_nothing():
    """Every Link is checked before any is bound, so a corrected storage can bind the mapping."""
    users, groups = users_mapping(), groups_mapping()

    with pytest.raises(ImproperlyConfigured, match="'Group'"):
        groups._bind(groups._targets({"User": users}))
    groups._bind(groups._targets({"User": users, "Group": groups}))
    user_link, _ = groups._collections[(Group, "members")]
    assert user_link.target is users


@pytest.mark.parametrize(
    "changes, message",
    [
        (
            {"emails": Many("emails", {"value": "value", "Value": "type"})},
            "'emails.value' and 'emails.Value' map the same attribute",
        ),
        (
            {"name.givenName": "given_name", "name": Link("manager", "User")},
            "'name.givenName' and 'name' map the same attribute",
        ),
        (
            {f"{ENTERPRISE}:manager.value": "title"},
            ":manager' and '.*:manager.value' map the same attribute",
        ),
    ],
    ids=[
        "entries",
        "attribute after its sub-attribute",
        "sub-attribute after its attribute",
    ],
)
def test_an_attribute_is_mapped_once(changes, message):
    """A second mapping of an attribute, or of a part of it, would silently replace the first."""
    with pytest.raises(ValueError, match=message):
        users_mapping(changes)
