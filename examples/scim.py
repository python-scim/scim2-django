from django.db.models import TextField
from django.db.models import Value
from django.db.models.functions import Concat
from django.db.models.functions import NullIf
from django.db.models.functions import Trim
from scim2_models import Bulk
from scim2_models import ChangePassword
from scim2_models import EnterpriseUser
from scim2_models import ETag
from scim2_models import Filter
from scim2_models import Group
from scim2_models import Patch
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_models import ServiceProviderConfig
from scim2_models import Sort
from scim2_models import User

from scim2_django import ScimSite
from scim2_django.storage import Computed
from scim2_django.storage import DjangoStorage
from scim2_django.storage import Link
from scim2_django.storage import Many
from scim2_django.storage import ResourceMapping

from .models import Account
from .models import Team

ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
MAX_RESULTS = 50
MAX_BULK_OPERATIONS = 100
MAX_BULK_PAYLOAD_SIZE = 1_048_576

USERS = ResourceMapping(
    User[EnterpriseUser],
    Account,
    {
        "id": "id",
        "externalId": "external_id",
        "userName": "login",
        "name.givenName": "first_name",
        "name.familyName": "last_name",
        "displayName": Computed(
            NullIf(
                Trim(
                    Concat(
                        "first_name", Value(" "), "last_name", output_field=TextField()
                    )
                ),
                Value(""),
                output_field=TextField(),
            )
        ),
        "active": "active",
        "emails": Many(
            "emails", {"value": "address", "type": "kind", "primary": "preferred"}
        ),
        "groups": Link("teams", "Group"),
        f"{ENTERPRISE}:employeeNumber": "employee_number",
        "meta.created": "created_at",
        "meta.lastModified": "updated_at",
    },
    version="revision",
)

GROUPS = ResourceMapping(
    Group,
    Team,
    {
        "id": "id",
        "externalId": "external_id",
        "displayName": "name",
        "members": Link("members", "User"),
        "meta.created": "created_at",
        "meta.lastModified": "updated_at",
    },
    version="revision",
)


def create_provider():
    return ScimProvider.from_discovery(
        [*USERS.schemas(), *GROUPS.schemas()],
        [
            ResourceType.from_resource(USERS.model),
            ResourceType.from_resource(GROUPS.model),
        ],
        config=ServiceProviderConfig(
            patch=Patch(supported=True),
            bulk=Bulk(
                supported=True,
                max_operations=MAX_BULK_OPERATIONS,
                max_payload_size=MAX_BULK_PAYLOAD_SIZE,
            ),
            filter=Filter(supported=True, max_results=MAX_RESULTS),
            change_password=ChangePassword(supported=False),
            sort=Sort(supported=True),
            etag=ETag(supported=True),
            authentication_schemes=[],
        ),
    )


def accept_anyone(request):
    return None


provider = create_provider()
storage = DjangoStorage({"User": USERS, "Group": GROUPS}, provider)
site = ScimSite(storage, provider, authenticate=accept_anyone)
