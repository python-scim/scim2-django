import pytest
from django.db import connection
from django.urls import path
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_models import User

from examples.transactions import AtomicStorage
from scim2_django import ScimSite

SCIM_JSON = "application/scim+json"
USER = {"schemas": [User.__schema__], "userName": "bjensen"}


class RecordingStorage(AtomicStorage):
    """Record whether each write runs in a transaction."""

    def __init__(self) -> None:
        super().__init__()
        self.in_transaction: list[bool] = []

    def create(self, resource_type, resource):
        self.in_transaction.append(connection.in_atomic_block)
        return super().create(resource_type, resource)


@pytest.mark.django_db(transaction=True)
def test_each_operation_runs_in_a_transaction(serve):
    storage = RecordingStorage()
    provider = ScimProvider(
        models=[User], resource_types=[ResourceType.from_resource(User)]
    )
    site = ScimSite(storage, provider, authenticate=lambda request: None)
    client = serve(path("scim/v2/", site.urls))

    response = client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)

    assert response.status_code == 201
    assert storage.in_transaction == [True]
    assert not connection.in_atomic_block
