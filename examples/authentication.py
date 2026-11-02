from scim2_models import AuthenticationScheme
from scim2_models import ScimProvider
from scim2_models import ServiceProviderConfig
from scim2_models import UnauthorizedException
from scim2_models import User
from scim2_server.memory import InMemoryStorage

from scim2_django import ScimSite

TOKENS = {"secret": "bjensen"}


def authenticate(request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if token not in TOKENS:
        raise UnauthorizedException(detail="Missing or invalid token")
    return TOKENS[token]


def create_provider():
    config = ServiceProviderConfig(
        authentication_schemes=[
            AuthenticationScheme(
                type=AuthenticationScheme.Type.oauthbearertoken,
                name="OAuth Bearer Token",
                description="Authentication with an OAuth 2.0 bearer token",
            )
        ]
    )
    return ScimProvider(models=[User], config=config)


site = ScimSite(
    InMemoryStorage(), create_provider(), name="protected", authenticate=authenticate
)
