import pytest
from scim2_server.testing import ScimStorageContract

from scim2_django.storage import DjangoStorage

from .mappings import groups_mapping
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
