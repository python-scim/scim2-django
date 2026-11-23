from typing import Any

from django.conf import settings
from django.db import models


class AbstractUserScim(models.Model):
    """What SCIM adds to the users of :setting:`AUTH_USER_MODEL`, in a model beside them.

    Inherit from it in a model of the project, and create its table with the
    migrations of the project. :func:`~scim2_django.auth.user_mapping` then
    maps the fields it keeps: ``externalId`` from ``external_id``, and
    ``meta.lastModified`` from ``last_modified``. Set a field to None in the
    model to leave it out. Add fields for other attributes, such as those of
    the enterprise extension, and map them through the relation, as in
    ``"scim__employee_number"``. These fields need null or a default, as the
    storage creates the record of a user at its first write.

    A user without record beside it has none of these values.
    """

    scim2_resource_type = "User"

    user: "models.OneToOneField[Any, Any]" = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="scim",
    )
    external_id: "models.TextField[str | None, str | None]" = models.TextField(
        null=True, db_index=True
    )
    last_modified: "models.DateTimeField[Any, Any]" = models.DateTimeField(null=True)

    class Meta:
        abstract = True


class AbstractGroupScim(models.Model):
    """What SCIM adds to the groups of :mod:`django.contrib.auth`, in a model beside them.

    Inherit from it in a model of the project, as for
    :class:`AbstractUserScim`. :func:`~scim2_django.auth.group_mapping` maps
    ``externalId`` from ``external_id``, ``meta.created`` from ``created``
    and ``meta.lastModified`` from ``last_modified``. Only the groups created
    through SCIM have a creation date.
    """

    scim2_resource_type = "Group"

    group: "models.OneToOneField[Any, Any]" = models.OneToOneField(
        "auth.Group", on_delete=models.CASCADE, primary_key=True, related_name="scim"
    )
    external_id: "models.TextField[str | None, str | None]" = models.TextField(
        null=True, db_index=True
    )
    created: "models.DateTimeField[Any, Any]" = models.DateTimeField(null=True)
    last_modified: "models.DateTimeField[Any, Any]" = models.DateTimeField(null=True)

    class Meta:
        abstract = True
