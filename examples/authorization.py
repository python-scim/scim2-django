from django.conf import settings
from scim2_models import ForbiddenException
from scim2_server.memory import InMemoryStorage
from scim2_server.routing import Operation
from scim2_server.service import ScimService

from examples.authentication import authenticate
from examples.authentication import create_provider
from scim2_django import ScimSite

READS = {Operation.query, Operation.search, Operation.search_with_body}


class ReadOnlyService(ScimService):
    def authorize(self, request, target, resource_type):
        if target.operation not in READS:
            raise ForbiddenException(detail="Read-only access")


provider = create_provider()
site = ScimSite(
    InMemoryStorage(),
    provider,
    service=ReadOnlyService(provider, secret=settings.SECRET_KEY),
    name="read_only",
    authenticate=authenticate,
)
