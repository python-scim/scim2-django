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
