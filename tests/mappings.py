from django.db.models import Case
from django.db.models import F
from django.db.models import TextField
from django.db.models import Value
from django.db.models import When
from django.db.models.functions import Cast
from django.db.models.functions import Concat
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import User

from scim2_django.storage import Link
from scim2_django.storage import Many
from scim2_django.storage import ResourceMapping
from scim2_django.storage import Transformed

from .models import GroupRecord
from .models import UserRecord

ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


def prefixed(prefix, field="id"):
    """Return the mapping of an identifier made of a prefix and the value of a field."""

    def to_field(value):
        if not value.startswith(prefix):
            raise ValueError(value)

        return value.removeprefix(prefix)

    expression = Concat(
        Value(prefix), Cast(field, TextField()), output_field=TextField()
    )
    return Transformed(field, expression=expression, to_field=to_field)


def hashed(value):
    """Return a password as a fake hash, which tells no value apart from no password."""
    return "!" if value is None else f"hash:{value}"


def hashed_password():
    """Return the mapping of a password stored as a fake hash, which the storage reads back."""
    expression = Case(
        When(password="!", then=None), default=F("password"), output_field=TextField()
    )
    return Transformed("password", expression=expression, to_field=hashed)


def users_mapping(changes=None, version="version"):
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
        "password": "password",
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
        User[EnterpriseUser], UserRecord, attributes, version=version
    )


def groups_mapping(changes=None, version="version"):
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
    return ResourceMapping(Group, GroupRecord, attributes, version=version)
