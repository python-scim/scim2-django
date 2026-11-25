import pytest
from django.core.exceptions import ValidationError
from django.db.models import F
from django.db.models.functions import Upper
from scim2_models import Group
from scim2_models import InvalidCursorException
from scim2_models import InvalidValueException
from scim2_models import Mutability
from scim2_models import NotFoundException
from scim2_models import PreconditionFailedException
from scim2_models import SearchRequest
from scim2_models import User

from scim2_django.storage import Computed
from scim2_django.storage import DjangoStorage
from scim2_django.storage import Many
from scim2_django.storage import ResourceMapping
from scim2_django.storage import Transformed

from .mappings import ENTERPRISE
from .mappings import groups_mapping
from .mappings import hashed
from .mappings import prefixed
from .mappings import users_mapping
from .models import GroupRecord
from .models import UserRecord
from .models import UserScim
from .test_storage import Server

pytestmark = pytest.mark.django_db


def lower(value):
    return None if value is None else value.lower()


def refuse(value):
    raise ValueError(value)


@pytest.fixture
def server():
    users = {
        "id": prefixed("u"),
        "password": Transformed("password", to_field=hashed),
        "nickName": Transformed(
            "nick_name", expression=Upper("nick_name"), to_field=lower
        ),
    }
    return Server(
        mappings={
            "User": users_mapping(users),
            "Group": groups_mapping({"id": prefixed("g")}),
        }
    )


def create_group(server, name, members=()):
    group = server.group_model.model_validate(
        {"displayName": name, "members": [{"value": value} for value in members]}
    )
    return server.storage.create(server.groups, group)


def group_names(page):
    return [resource.display_name for resource in page.resources]


def test_an_identifier_is_read_through_its_expression(server):
    """Users and groups never share an identifier, although each type counts its own keys."""
    group = create_group(server, "Admins")
    user = server.create_user("bjensen")

    assert group.id == f"g{GroupRecord.objects.get().pk}"
    assert user.id.startswith("u")
    assert server.storage.get(server.groups, group.id).id == group.id


@pytest.mark.parametrize("text", ["{pk}", "x{pk}", "g0{pk}", "gx"])
def test_an_identifier_the_expression_does_not_give_finds_nothing(server, text):
    """The prefix is checked, and so is the exact text: "g01" is not the identifier "g1"."""
    pk = GroupRecord.objects.create(display_name="Admins").pk

    with pytest.raises(NotFoundException):
        server.storage.get(server.groups, text.format(pk=pk))


@pytest.mark.parametrize(
    ("scim_filter", "expected"),
    [
        ('id eq "g7"', ["Admins"]),
        ('id eq "g07"', []),
        ('id eq "x7"', []),
        ('id ne "g07"', ["Admins", "Staff"]),
        ('id ne "g7"', ["Staff"]),
        ('id sw "g"', ["Admins", "Staff"]),
        ('id co "7"', ["Admins"]),
    ],
)
def test_a_filter_compares_the_identifier_clients_read(server, scim_filter, expected):
    GroupRecord.objects.create(display_name="Admins", pk=7)
    GroupRecord.objects.create(display_name="Staff", pk=300)

    page = server.storage.search([server.groups], SearchRequest(filter=scim_filter))

    assert sorted(group_names(page)) == expected


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
def test_a_sort_on_the_identifier_orders_its_text(server, sort_order):
    """The identifier "g10" comes before "g9", as SearchRequest.sort orders strings."""
    for pk, name in [(9, "nine"), (10, "ten"), (100, "hundred")]:
        GroupRecord.objects.create(pk=pk, display_name=name)
    search_request = SearchRequest(sort_by="id", sort_order=sort_order)
    everyone = server.storage.search([server.groups], SearchRequest()).resources

    page = server.storage.search([server.groups], search_request)

    assert group_names(page) == [r.display_name for r in search_request.sort(everyone)]


def test_cursors_page_on_transformed_identifiers(server):
    """The ties of the pages are the primary keys, found again from the identifiers."""
    for pk in [9, 10, 100]:
        GroupRecord.objects.create(pk=pk, display_name="Team")
    search_request = SearchRequest(cursor="", count=1)

    pages = [server.storage.search([server.groups], search_request)]
    while pages[-1].next is not None and len(pages) < 5:
        pages.append(
            server.storage.search(
                [server.groups], search_request, position=pages[-1].next
            )
        )

    assert [page.resources[0].id for page in pages] == ["g9", "g10", "g100"]


def test_a_cursor_on_an_identifier_to_field_refuses_is_invalid(server):
    position = {"d": "next", "k": None, "i": "x1"}

    with pytest.raises(InvalidCursorException):
        server.storage.search(
            [server.groups], SearchRequest(cursor="x"), position=position
        )


def test_a_search_at_the_root_reads_every_identifier(server):
    group = create_group(server, "Admins")
    user = server.create_user("bjensen")

    page = server.storage.search(
        [server.users, server.groups], SearchRequest(sort_by="id")
    )

    assert [r.id for r in page.resources] == [group.id, user.id]


def test_members_are_found_by_their_identifier(server):
    user = server.create_user("bjensen")
    admins = create_group(server, "Admins")

    staff = create_group(server, "Staff", [user.id, admins.id])

    assert sorted(member.value for member in staff.members) == sorted(
        [user.id, admins.id]
    )


@pytest.mark.parametrize("text", ["g0{pk}", "x{pk}"])
def test_a_member_the_expression_does_not_give_is_refused(server, text):
    """The value "g01" does not link to the group "g1", even along with it."""
    admins = create_group(server, "Admins")
    value = text.format(pk=admins.id.removeprefix("g"))

    with pytest.raises(InvalidValueException):
        create_group(server, "Staff", [admins.id, value])


def test_an_update_keeps_the_unique_values_of_the_resource(server):
    """The resource is told apart from the others by its identifier."""
    user = server.create_user("bjensen")
    user.display_name = "Babs"

    updated = server.storage.update(server.users, user)

    assert updated.display_name == "Babs"


def test_a_password_is_written_through_to_field(server):
    user = server.storage.create(
        server.users, server.user_model(user_name="bjensen", password="t1meMa$heen")
    )

    assert user.password is None
    assert UserRecord.objects.get().password == "hash:t1meMa$heen"


def test_a_new_resource_without_value_gets_what_to_field_gives_for_none(server):
    server.create_user("bjensen")

    assert UserRecord.objects.get().password == "!"


def test_an_update_without_password_keeps_it(server):
    user = server.storage.create(
        server.users, server.user_model(user_name="bjensen", password="secret")
    )
    user.display_name = "Babs"

    server.storage.update(server.users, user)

    assert UserRecord.objects.get().password == "hash:secret"


def test_a_value_to_field_refuses_is_invalid_and_left_out_of_the_error():
    server = Server(
        mappings={
            "User": users_mapping(
                {"password": Transformed("password", to_field=refuse)}
            )
        }
    )

    with pytest.raises(InvalidValueException) as error:
        server.storage.create(
            server.users, server.user_model(user_name="bjensen", password="secret")
        )

    assert "secret" not in str(error.value.detail)
    assert not UserRecord.objects.exists()


def test_a_readable_value_is_read_filtered_and_sorted_through_its_expression(server):
    for user_name, nick_name in [("a", "Bob"), ("b", None), ("c", "alice")]:
        server.storage.create(
            server.users, server.user_model(user_name=user_name, nick_name=nick_name)
        )

    found = server.storage.search(
        [server.users], SearchRequest(filter='nickName eq "ALICE"')
    )
    ordered = server.storage.search([server.users], SearchRequest(sort_by="nickName"))

    assert [r.nick_name for r in found.resources] == ["ALICE"]
    assert [r.user_name for r in ordered.resources] == ["c", "a", "b"]
    assert set(UserRecord.objects.values_list("nick_name", flat=True)) == {
        "alice",
        "bob",
        None,
    }


def test_an_update_removing_a_value_writes_what_to_field_gives_for_none(server):
    user = server.storage.create(
        server.users, server.user_model(user_name="bjensen", nick_name="Babs")
    )
    user.nick_name = None

    updated = server.storage.update(server.users, user)

    assert updated.nick_name is None
    assert UserRecord.objects.get().nick_name is None


def test_a_field_beside_the_resource_is_transformed():
    """The expression reads the related record, and to_field writes it."""
    division = Transformed(
        "scim__division", expression=Upper("scim__division"), to_field=lower
    )
    server = Server(
        mappings={"User": users_mapping({f"{ENTERPRISE}:division": division})}
    )
    user = server.create_user("bjensen")
    assert not UserScim.objects.exists()

    data = user.model_dump()
    data[ENTERPRISE] = {"division": "R&D"}
    updated = server.storage.update(server.users, type(user).model_validate(data))

    assert updated.model_dump()[ENTERPRISE]["division"] == "R&D"
    assert UserScim.objects.get().division == "r&d"


def test_a_transformed_field_without_to_field_is_read_only():
    users = users_mapping({"title": Transformed("title", expression=Upper("title"))})

    (schema, _) = users.schemas()
    (title,) = [a for a in schema.attributes if a.name == "title"]

    assert title.mutability == Mutability.read_only


def test_a_transformed_field_needs_an_expression_or_to_field():
    with pytest.raises(ValueError, match="needs expression or to_field"):
        Transformed("title")


@pytest.mark.parametrize(
    "transformed",
    [
        Transformed("id", to_field=str),
        Transformed("id", expression=F("id")),
    ],
    ids=["without expression", "without to_field"],
)
def test_a_transformed_identifier_needs_both(transformed):
    """The storage finds a resource by its identifier through to_field."""
    with pytest.raises(ValueError, match="needs an expression and to_field"):
        users_mapping({"id": transformed})


def test_a_date_cannot_be_transformed():
    created = Transformed("created", expression=F("created"), to_field=str)

    with pytest.raises(ValueError, match="must be mapped to the name of a field"):
        users_mapping({"meta.created": created})


def test_an_entry_cannot_be_transformed():
    emails = Many("emails", {"value": Transformed("value", to_field=str)})

    with pytest.raises(ValueError, match="cannot be computed nor transformed"):
        users_mapping({"emails": emails})


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (
            Transformed("nope", expression=F("title"), to_field=str),
            "scim2_django.E001",
        ),
        (
            Transformed("manager", expression=F("title"), to_field=str),
            "scim2_django.E002",
        ),
        (
            Transformed("title", expression=F("badge_number"), to_field=str),
            "scim2_django.E008",
        ),
        (
            Transformed("title", expression=F("nope"), to_field=str),
            "scim2_django.E009",
        ),
    ],
    ids=["unknown field", "relation", "expression without text", "unknown expression"],
)
def test_a_transformed_field_is_checked(value, error):
    assert [message.id for message in users_mapping({"title": value}).check()] == [
        error
    ]


def test_a_transformed_identifier_needs_a_unique_field():
    users = ResourceMapping(
        User,
        UserRecord,
        {"id": prefixed("u", "title"), "userName": "user_name"},
        version="version",
    )

    assert [message.id for message in users.check()] == ["scim2_django.E005"]


def test_the_expression_of_an_identifier_gives_text():
    """Per RFC 7643 §3.1, an identifier is a string."""
    groups = ResourceMapping(
        Group,
        GroupRecord,
        {
            "id": Transformed("id", expression=F("id"), to_field=int),
            "displayName": "display_name",
        },
        version="version",
    )

    assert [message.id for message in groups.check()] == ["scim2_django.E008"]


def test_a_value_to_field_gives_as_none_is_required_by_a_field_without_null():
    """The preferred language has no null, and to_field gives None for no value."""
    language = Transformed(
        "preferred_language", expression=Upper("preferred_language"), to_field=lower
    )
    server = Server(mappings={"User": users_mapping({"preferredLanguage": language})})

    with pytest.raises(InvalidValueException, match="required"):
        server.create_user("bjensen")

    user = server.storage.create(
        server.users, server.user_model(user_name="bjensen", preferred_language="FR")
    )
    assert user.preferred_language == "FR"


def test_a_validation_error_of_to_field_refuses_the_value():
    def validated(value):
        raise ValidationError("too short")

    password = Transformed("password", to_field=validated)
    server = Server(mappings={"User": users_mapping({"password": password})})

    with pytest.raises(InvalidValueException):
        server.storage.create(
            server.users, server.user_model(user_name="bjensen", password="123")
        )


@pytest.mark.parametrize(
    "division",
    [
        Computed(Upper("scim__division")),
        Transformed(
            "scim__division", expression=Upper("scim__division"), to_field=lower
        ),
    ],
    ids=["computed", "transformed"],
)
def test_a_write_computes_the_expressions_again_once_locked(division):
    """Another transaction changed the record beside the user while this one waited for the lock.

    The hash of the user then covers the new division, so the write is refused.
    """

    class Waiting(DjangoStorage):
        def _lock_related(self, relations, record):
            UserScim.objects.update(division="sales")
            super()._lock_related(relations, record)

    users = users_mapping({f"{ENTERPRISE}:division": division}, version=None)
    server = Server(
        storage_class=Waiting,
        mappings={"User": users, "Group": groups_mapping(version=None)},
    )
    user = server.create_user("bjensen")
    UserScim.objects.create(user=UserRecord.objects.get(), division="r&d")
    user = server.storage.get(server.users, user.id)

    with pytest.raises(PreconditionFailedException):
        server.storage.update(server.users, user, expected_version=user.meta.version)
