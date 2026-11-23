from typing import Any

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models.signals import m2m_changed
from django.db.models.signals import post_save

from ..storage.storage import _now
from . import _field
from . import _groups
from . import _relation_beside

_UNLINKED = "_scim2_unlinked"
"""The attribute keeping the records a removal or a clear unlinks, until it is done."""


def connect() -> None:
    """Date the records beside the users and the groups whenever they change, outside SCIM too.

    The saves of proxy models are dated too. Nothing is connected for the
    links of a user model without groups.
    """
    post_save.connect(_saved, dispatch_uid="scim2_django.auth.saved")
    groups = _groups(get_user_model())
    if groups is not None:
        m2m_changed.connect(
            _groups_changed,
            sender=groups.remote_field.through,
            dispatch_uid="scim2_django.auth.groups",
        )


def _group_model() -> type[models.Model]:
    return apps.get_model("auth", "Group")


def _saved(
    sender: type[models.Model],
    instance: models.Model,
    raw: bool,
    using: str,
    update_fields: Any,
    **kwargs: Any,
) -> None:
    """Date a saved user or group.

    A record loaded from a fixture keeps the dates of the fixture, and a
    save of the last login changes nothing SCIM returns.
    """
    if raw or (update_fields is not None and set(update_fields) == {"last_login"}):
        return

    _date(sender, {instance.pk}, using)


def _groups_changed(
    sender: type[models.Model],
    instance: models.Model,
    action: str,
    reverse: bool,
    model: type[models.Model],
    pk_set: set[Any] | None,
    using: str,
    **kwargs: Any,
) -> None:
    """Date the users and the groups whose links change: a group lists its members, and a user its groups.

    A removal and a clear may name records that were not linked, or none,
    so the records they unlink are kept before them. A change linking or
    unlinking nothing dates nothing.
    """
    if action in ("pre_remove", "pre_clear"):
        linked = set(_linked(instance, reverse, model, using))
        if pk_set is not None:
            linked &= pk_set

        setattr(instance, _UNLINKED, linked)
        return

    if action in ("post_remove", "post_clear"):
        pk_set = getattr(instance, _UNLINKED)
        delattr(instance, _UNLINKED)
    elif action != "post_add":
        return

    if not pk_set:
        return

    _date(type(instance), {instance.pk}, using)
    _date(model, pk_set, using)


def _linked(
    instance: models.Model, reverse: bool, model: type[models.Model], using: str
) -> Any:
    """Return the primary keys of the records linked to a user, or to a group, through the groups of the user."""
    groups = _groups(get_user_model())
    lookup = groups.name if reverse else groups.related_query_name()
    records = model._base_manager.using(using)
    return records.filter(**{lookup: instance}).values_list("pk", flat=True)


def _date(record: type[models.Model], keys: set[Any], using: str) -> None:
    """Set the modification date of the records beside some users or groups, creating those that miss.

    A record of another model than the user model and the group model, or of
    a model without model beside it, is not dated.
    """
    record = record._meta.concrete_model  # type: ignore[assignment]
    if record is _group_model():
        resource_type = "Group"
    elif record is get_user_model():
        resource_type = "User"
    else:
        return

    relation = _relation_beside(record, resource_type)
    if relation is None or _field(relation.related_model, "last_modified") is None:
        return

    beside = relation.related_model
    now = _now()
    records = beside._base_manager.using(using)
    existing = set(records.filter(pk__in=keys).values_list("pk", flat=True))
    records.filter(pk__in=existing).update(last_modified=now)
    records.bulk_create(
        [
            beside(**{relation.field.attname: key, "last_modified": now})
            for key in keys - existing
        ],
        ignore_conflicts=True,
    )
