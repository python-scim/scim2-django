import pytest
from scim2_server.memory import InMemoryStorage

from examples import scim


@pytest.fixture(autouse=True)
def example_server(db):
    """Run each page against an example server with no resource.

    The WSGI handler of Django checks the database connection on each
    request, so the pages get access to the database.
    """
    storage = scim.site.storage
    scim.site.storage = scim.site.handler.storage = InMemoryStorage()
    yield
    scim.site.storage = scim.site.handler.storage = storage
