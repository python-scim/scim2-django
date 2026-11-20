from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import User

from scim2_django.storage import Attribute
from scim2_django.storage import Link
from scim2_django.storage import Many
from scim2_django.storage import ResourceMapping

from .models import GroupRecord
from .models import UserRecord

ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


def users_mapping(changes=None):
    """Return a new mapping of the users, with some paths changed, or removed when None."""
    attributes = {
        "id": "id",
        "externalId": "external_id",
        "userName": "user_name",
        "displayName": "display_name",
        "title": "title",
        "userType": "user_type",
        "nickName": "nick_name",
        "preferredLanguage": "preferred_language",
        "locale": "lower_name",
        "name.givenName": "given_name",
        "name.familyName": "family_name",
        "active": "active",
        "password": Attribute("password", readable=False),
        "emails": Many(
            "emails", {"value": "value", "type": "type", "primary": "primary"}
        ),
        "groups": Link("groups", "Group", type="direct"),
        f"{ENTERPRISE}:employeeNumber": "employee_number",
        f"{ENTERPRISE}:manager": Link("manager", "User"),
        "meta.created": "created",
        "meta.lastModified": "last_modified",
    }
    attributes |= changes or {}
    attributes = {
        path: value for path, value in attributes.items() if value is not None
    }
    return ResourceMapping(
        User[EnterpriseUser], UserRecord, attributes, version="version"
    )


def groups_mapping(changes=None):
    """Return a new mapping of the groups, with some paths changed, or removed when None."""
    attributes = {
        "id": "id",
        "externalId": "external_id",
        "displayName": "display_name",
        "members": [
            Link("members", "User", type="User"),
            Link("subgroups", "Group", type="Group"),
        ],
        "meta.created": "created",
        "meta.lastModified": "last_modified",
    }
    attributes |= changes or {}
    attributes = {
        path: value for path, value in attributes.items() if value is not None
    }
    return ResourceMapping(Group, GroupRecord, attributes, version="version")
