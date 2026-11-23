from scim2_models import ChangePassword
from scim2_models import ETag
from scim2_models import Filter
from scim2_models import Patch
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_models import ServiceProviderConfig
from scim2_models import Sort

from scim2_django import ScimSite
from scim2_django.auth import group_mapping
from scim2_django.auth import user_mapping
from scim2_django.storage import DjangoStorage

MAX_RESULTS = 50


def create_provider(users, groups):
    return ScimProvider.from_discovery(
        [*users.schemas(), *groups.schemas()],
        [
            ResourceType.from_resource(users.model),
            ResourceType.from_resource(groups.model),
        ],
        config=ServiceProviderConfig(
            patch=Patch(supported=True),
            filter=Filter(supported=True, max_results=MAX_RESULTS),
            change_password=ChangePassword(supported=True),
            sort=Sort(supported=True),
            etag=ETag(supported=True),
            authentication_schemes=[],
        ),
    )


def accept_anyone(request):
    return None


users = user_mapping()
groups = group_mapping()
provider = create_provider(users, groups)
storage = DjangoStorage({"User": users, "Group": groups}, provider)
site = ScimSite(storage, provider, name="accounts", authenticate=accept_anyone)
