import pytest
from scim2_server.testing import ScimStorageContract

from scim2_django.storage import DjangoStorage

from .mappings import ENTERPRISE
from .mappings import groups_mapping
from .mappings import hashed_password
from .mappings import prefixed
from .mappings import users_mapping

pytestmark = pytest.mark.django_db


class TestDjangoStorage(ScimStorageContract):
    @pytest.fixture
    def storage(self, provider):
        mappings = {"User": users_mapping(), "Group": groups_mapping()}
        return DjangoStorage(mappings, provider)


class TestDjangoStorageWithoutDates(ScimStorageContract):
    """Resources whose models have no date."""

    stores_created = False
    stores_last_modified = False

    @pytest.fixture
    def storage(self, provider):
        undated = {"meta.created": None, "meta.lastModified": None}
        mappings = {"User": users_mapping(undated), "Group": groups_mapping(undated)}
        return DjangoStorage(mappings, provider)


class TestDjangoStorageWithHashes(ScimStorageContract):
    """Resources whose models have neither a version field nor dates."""

    stores_created = False
    stores_last_modified = False

    @pytest.fixture
    def storage(self, provider):
        undated = {"meta.created": None, "meta.lastModified": None}
        mappings = {
            "User": users_mapping(undated, version=None),
            "Group": groups_mapping(undated, version=None),
        }
        return DjangoStorage(mappings, provider)


class TestDjangoStorageWithRelatedFields(ScimStorageContract):
    """Users whose externalId and division are in a table beside them."""

    @pytest.fixture
    def storage(self, provider):
        beside = {
            "externalId": "scim__external_id",
            f"{ENTERPRISE}:division": "scim__division",
        }
        mappings = {"User": users_mapping(beside), "Group": groups_mapping()}
        return DjangoStorage(mappings, provider)


class TestDjangoStorageWithTransformedFields(ScimStorageContract):
    """Identifiers made of a prefix and the primary key, and hashed passwords."""

    @pytest.fixture
    def storage(self, provider):
        users = {
            "id": prefixed("u"),
            "password": hashed_password(),
        }
        mappings = {
            "User": users_mapping(users),
            "Group": groups_mapping({"id": prefixed("g")}),
        }
        return DjangoStorage(mappings, provider)


class TestDjangoStorageWithRelatedDates(ScimStorageContract):
    """Users whose dates are in a table beside them."""

    @pytest.fixture
    def storage(self, provider):
        beside = {
            "meta.created": "scim__created",
            "meta.lastModified": "scim__last_modified",
        }
        mappings = {"User": users_mapping(beside), "Group": groups_mapping()}
        return DjangoStorage(mappings, provider)
