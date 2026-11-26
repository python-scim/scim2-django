import datetime

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.db import connection
from django.db.models import F
from django.urls import path
from scim2_models import ConflictException
from scim2_models import Group
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import InvalidValueException
from scim2_models import NotFoundException
from scim2_models import PreconditionFailedException
from scim2_models import ResourceType
from scim2_models import ScimPolicy
from scim2_models import ScimProvider
from scim2_models import SearchRequest
from scim2_models import UniquenessException
from scim2_models import User
from scim2_server.utils import load_default_provider
from scim2_server.utils import load_default_service_provider_config

from scim2_django import ScimSite
from scim2_django.storage import DjangoStorage
from scim2_django.storage import ResourceMapping
from scim2_django.storage.comparison import _vendor

from .mappings import groups_mapping
from .mappings import users_mapping
from .models import AliasRecord
from .models import BadgeRecord
from .models import EmailRecord
from .models import Locker
from .models import LogRecord
from .models import PassRecord
from .models import Person
from .models import UserRecord
from .models import Volunteer
from .test_mapping import OWNED
from .test_mapping import Owned

pytestmark = pytest.mark.django_db

SCIM_JSON = "application/scim+json"
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


@pytest.mark.parametrize(
    "cause", [ValueError("check"), None], ids=["database", "project"]
)
def test_another_integrity_error_goes_up(server, monkeypatch, cause):
    """An error that neither the uniqueness, a unique index nor a foreign key explains is not hidden."""

    def refuse(*args, **kwargs):
        raise IntegrityError("CHECK constraint failed") from cause

    monkeypatch.setattr(UserRecord, "save", refuse)

    with pytest.raises(IntegrityError):
        server.create_user("bjensen")


@pytest.mark.parametrize(
    "model", [BadgeRecord, PassRecord], ids=["protect", "restrict"]
)
def test_a_user_another_record_refers_to_cannot_be_deleted(server, model):
    """A record refers to the user through a PROTECT or RESTRICT foreign key."""
    user = server.create_user("bjensen")
    model.objects.create(user_id=user.id)

    with pytest.raises(ConflictException, match="Another record refers"):
        server.storage.delete(server.users, user.id)

    assert UserRecord.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_a_user_a_foreign_key_of_the_database_refers_to_cannot_be_deleted():
    """The database refuses the deletion, on PostgreSQL and SQLite when the operation commits."""
    server = Server()
    user = server.create_user("bjensen")
    LogRecord.objects.create(user_id=user.id)

    with pytest.raises(ConflictException, match="Another record refers"):
        with server.storage.operation():
            server.storage.delete(server.users, user.id)

    assert UserRecord.objects.exists()


def test_an_update_removing_an_entry_another_record_refers_to_is_a_conflict(server):
    """An alias protects the email of the user."""
    user = server.storage.create(
        server.users,
        server.user_model(
            user_name="bjensen", emails=[{"value": "bjensen@example.com"}]
        ),
    )
    AliasRecord.objects.create(email=EmailRecord.objects.get())
    user.emails = None

    with pytest.raises(ConflictException, match="Another record refers"):
        server.storage.update(server.users, user)

    assert EmailRecord.objects.exists()


def test_an_integrity_error_without_a_cause_goes_up(server):
    """Code of the project, rather than the database, may raise it."""
    with pytest.raises(IntegrityError, match="refused by the project"):
        with server.storage.operation():
            raise IntegrityError("refused by the project")


class Sqlstate(Exception):
    sqlstate = "23503"


class Pgcode(Exception):
    pgcode = "23503"


class SqliteError(Exception):
    sqlite_errorname = "SQLITE_CONSTRAINT_FOREIGNKEY"


@pytest.mark.parametrize(
    "cause",
    [
        Sqlstate(),
        Pgcode(),
        SqliteError(),
        *(ValueError(code) for code in (1216, 1217, 1451, 1452)),
    ],
    ids=["psycopg", "psycopg2", "sqlite", "1216", "1217", "1451", "1452"],
)
def test_a_write_a_foreign_key_refuses_is_a_conflict(server, monkeypatch, cause):
    """The user links to a manager deleted in the meantime."""

    def refuse(*args, **kwargs):
        raise IntegrityError("FOREIGN KEY constraint failed") from cause

    monkeypatch.setattr(UserRecord, "save", refuse)

    with pytest.raises(ConflictException, match="Another record refers"):
        server.create_user("bjensen")


def test_another_integrity_error_of_a_deletion_goes_up(server, monkeypatch):
    user = server.create_user("bjensen")

    def refuse(*args, **kwargs):
        raise IntegrityError("CHECK constraint failed") from ValueError("check")

    monkeypatch.setattr(UserRecord, "delete", refuse)

    with pytest.raises(IntegrityError):
        server.storage.delete(server.users, user.id)


@pytest.mark.parametrize(
    "cause,error",
    [(ValueError("check"), IntegrityError), (Sqlstate(), ConflictException)],
    ids=["other", "foreign key"],
)
def test_an_integrity_error_leaving_an_operation_is_a_conflict_for_a_foreign_key(
    server, cause, error
):
    with pytest.raises(error):
        with server.storage.operation():
            raise IntegrityError("constraint failed") from cause


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


@pytest.mark.filterwarnings("ignore:The .* storage cannot compare")
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
    if _vendor(connection) == "mariadb":
        pytest.skip("The unique index of MariaDB ignores the case, as the column does")

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


@pytest.mark.parametrize("text", ["0{id}", "+{id}", " {id}"])
def test_a_resource_is_found_by_the_exact_text_of_its_identifier(server, text):
    """The text "01" is not the identifier "1", whose field reads it as 1, as filters have it."""
    group = server.create_group([])
    value = text.format(id=group.id)

    with pytest.raises(NotFoundException):
        server.storage.get(server.groups, value)

    with pytest.raises(InvalidValueException):
        server.create_group([{"value": value, "type": "Group"}])


def test_a_value_a_unique_index_refuses_is_not_unique():
    """The serial number is unique in the database, although SCIM lets two users share an externalId."""
    server = Server(mappings={"User": users_mapping({"externalId": "serial"})})
    server.storage.create(
        server.users, server.user_model(user_name="a", external_id="1")
    )

    with pytest.raises(UniquenessException):
        server.storage.create(
            server.users, server.user_model(user_name="b", external_id="1")
        )


def test_a_value_the_collation_of_a_unique_column_ignores_is_not_unique(server):
    """The default collation of MariaDB ignores the accents, so its unique index refuses José next to Jose."""
    if _vendor(connection) != "mariadb":
        pytest.skip("Only MariaDB ignores the accents in the unique index")

    server.create_user("Jose")

    with pytest.raises(UniquenessException):
        server.create_user("José")


@pytest.fixture
def people():
    """Return a server of people, whose identifiers are text: abc, b and C."""
    for pk in ["abc", "b", "C"]:
        Person.objects.create(id=pk, user_name=pk, created=NOW, last_modified=NOW)

    people = ResourceMapping(
        User,
        Person,
        {
            "id": "id",
            "userName": "user_name",
            "meta.created": "created",
            "meta.lastModified": "last_modified",
        },
        version="version",
    )
    return Server(mappings={"User": people, "Group": groups_mapping({"members": None})})


@pytest.mark.parametrize("text", ["ABC", "abc "])
def test_a_text_identifier_is_found_by_its_exact_text(people, text):
    """A collation ignoring the case, or the trailing spaces, does not find another identifier."""
    assert people.storage.get(people.users, "abc").id == "abc"
    with pytest.raises(NotFoundException):
        people.storage.get(people.users, text)


def test_cursors_page_once_through_text_identifiers(people):
    """The identifiers order the people a sort leaves tied, as the anchors of the cursors compare them."""
    search_request = SearchRequest(cursor="", count=1, sort_by="meta.created")
    pages = [people.storage.search([people.users], search_request)]
    while pages[-1].next is not None and len(pages) < 5:
        pages.append(
            people.storage.search(
                [people.users], search_request, position=pages[-1].next
            )
        )

    assert sorted(r.id for page in pages for r in page.resources) == ["C", "abc", "b"]


@pytest.fixture
def volunteers(serve):
    """Return a client of a server publishing the schemas of its mapping, where active is required."""
    mapping = ResourceMapping(
        User,
        Volunteer,
        {"id": "id", "userName": "user_name", "active": "active"},
        version="version",
    )
    provider = ScimProvider.from_discovery(
        mapping.schemas(),
        [ResourceType.from_resource(User)],
        config=load_default_service_provider_config(),
    )
    site = ScimSite(
        DjangoStorage({"User": mapping}, provider),
        provider,
        authenticate=lambda request: None,
    )
    return serve(path("scim/v2/", site.urls))


def scim_type(response):
    return (response.status_code, response.json().get("scimType"))


def test_a_write_without_a_required_value_is_refused(volunteers):
    """Each write without whether the volunteer is active is refused."""
    user = {"schemas": [User.__schema__], "userName": "bjensen"}

    response = volunteers.post("/scim/v2/Users", user, content_type=SCIM_JSON)
    assert response.status_code == 400

    response = volunteers.post(
        "/scim/v2/Users", user | {"active": True}, content_type=SCIM_JSON
    )
    assert response.status_code == 201
    location = f"/scim/v2/Users/{response.json()['id']}"

    response = volunteers.put(location, user, content_type=SCIM_JSON)
    assert scim_type(response) == (400, "invalidValue")

    remove = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "remove", "path": "active"}],
    }
    response = volunteers.patch(location, remove, content_type=SCIM_JSON)
    assert scim_type(response) == (400, "mutability")
    assert Volunteer.objects.get().active is True


@pytest.mark.parametrize(
    "search_request,error",
    [
        (SearchRequest(filter=f"{OWNED}:secret eq 7"), InvalidFilterException),
        (SearchRequest(sort_by=f"{OWNED}:secret"), InvalidPathException),
    ],
    ids=["filter", "sort"],
)
def test_a_value_never_returned_cannot_be_searched(search_request, error):
    """A client could guess the secret by filtering or sorting on it."""
    lockers = ResourceType.from_resource(User[Owned])
    mapping = ResourceMapping(
        User[Owned], Locker, {"id": "id", f"{OWNED}:secret": "code"}
    )
    storage = DjangoStorage({"User": mapping})

    with pytest.raises(error):
        storage.search([lockers], search_request)
