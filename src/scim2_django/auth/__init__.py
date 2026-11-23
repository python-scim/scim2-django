from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

from django.apps import apps as registry
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import FieldDoesNotExist
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Model
from django.db.models import TextField
from django.db.models import UUIDField
from django.db.models import Value
from django.db.models.functions import Cast
from django.db.models.functions import Concat
from scim2_models import Group
from scim2_models import Resource
from scim2_models import User

from ..storage import Entry
from ..storage import Link
from ..storage import ResourceMapping
from ..storage import Transformed
from ..storage.mapping import _UUIDText


def user_mapping(
    attributes: Mapping[str, Any] | None = None,
    *,
    model: type[Resource[Any]] = User,
    prefix: str = "u",
) -> ResourceMapping:
    """Return the mapping of the users of the project, the records of :setting:`AUTH_USER_MODEL`.

    The mapping holds the attributes the user model has fields for:

    - ``id``, the primary key after ``prefix``, such as ``u12``;
    - ``userName``, the field named by
      :attr:`~django.contrib.auth.models.CustomUser.USERNAME_FIELD`;
    - ``name.givenName`` and ``name.familyName``, from ``first_name`` and
      ``last_name``;
    - ``emails``, one ``work`` and ``primary`` address, from the field
      :meth:`~django.contrib.auth.models.AbstractBaseUser.get_email_field_name`
      gives, unless it holds the user name;
    - ``active``, from ``is_active``;
    - ``password``, checked by the validators of
      :setting:`AUTH_PASSWORD_VALIDATORS`, written with
      :func:`~django.contrib.auth.hashers.make_password`, and never read. A
      user created without password, or with an empty one, gets an unusable
      one;
    - ``groups``, the groups of :mod:`django.contrib.auth`, from ``groups``;
    - ``meta.created``, from ``date_joined``.

    ``meta.version`` is a hash of the user.

    :param attributes: Attributes to add or to change, by SCIM path, as
        :class:`~scim2_django.storage.ResourceMapping` takes them. None
        removes an attribute. A path replaces the attributes it holds, or that
        hold it: ``"name"`` replaces ``name.givenName`` and ``name.familyName``.
    :param model: The SCIM model of the users, such as ``User[EnterpriseUser]``
        to map attributes of the enterprise extension.
    :param prefix: The prefix of the ``id``, which tells users from groups.
        An empty prefix makes the ``id`` the primary key, for a user model
        whose primary key is a UUID. Choose it before the first identity
        provider stores the ``id``: changing it changes every ``id``.
    :returns: The mapping of the users, as the resource type ``User``.
    """
    record: Any = get_user_model()
    mapped: dict[str, Any] = {
        "id": _identifier(record, prefix),
        "userName": record.USERNAME_FIELD,
        "password": Transformed("password", to_field=_password),
    }
    for path, name in (
        ("name.givenName", "first_name"),
        ("name.familyName", "last_name"),
        ("active", "is_active"),
        ("meta.created", "date_joined"),
    ):
        if _field(record, name) is not None:
            mapped[path] = name

    email = record.get_email_field_name()
    if email != record.USERNAME_FIELD and _field(record, email) is not None:
        mapped["emails"] = Entry({"value": email}, type="work", primary=True)

    if _groups(record) is not None:
        mapped["groups"] = Link("groups", "Group", type="direct")

    mapped |= _beside(
        record,
        "User",
        {"externalId": "external_id", "meta.lastModified": "last_modified"},
    )
    return _mapping(model, record, mapped, attributes)


def group_mapping(
    attributes: Mapping[str, Any] | None = None,
    *,
    model: type[Resource[Any]] = Group,
    prefix: str = "g",
) -> ResourceMapping:
    """Return the mapping of the groups of :mod:`django.contrib.auth`.

    The mapping holds:

    - ``id``, the primary key after ``prefix``, such as ``g3``;
    - ``displayName``, from ``name``;
    - ``members``, the users of the group, when the user model has
      ``groups`` with a reverse relation.

    ``meta.version`` is a hash of the group.

    :param attributes: Attributes to add or to change, by SCIM path, as
        :class:`~scim2_django.storage.ResourceMapping` takes them. None
        removes an attribute. A path replaces the attributes it holds, or that
        hold it.
    :param model: The SCIM model of the groups.
    :param prefix: The prefix of the ``id``, which tells groups from users.
        Choose it before the first identity provider stores the ``id``:
        changing it changes every ``id``.
    :returns: The mapping of the groups, as the resource type ``Group``.
    """
    record = registry.get_model("auth", "Group")
    mapped: dict[str, Any] = {
        "id": _identifier(record, prefix),
        "displayName": "name",
    }
    groups = _groups(get_user_model())
    if groups is not None and not groups.remote_field.hidden:
        mapped["members"] = Link(groups.related_query_name(), "User", type="User")

    mapped |= _beside(
        record,
        "Group",
        {
            "externalId": "external_id",
            "meta.created": "created",
            "meta.lastModified": "last_modified",
        },
    )
    return _mapping(model, record, mapped, attributes)


def _mapping(
    model: type[Resource[Any]],
    record: type[Model],
    mapped: dict[str, Any],
    attributes: Mapping[str, Any] | None,
) -> ResourceMapping:
    """Return the mapping of the attributes, changed by the attributes of the project.

    A path of the project replaces the attributes it holds, or that hold it.
    """
    changes = attributes or {}
    kept = {
        path: value
        for path, value in mapped.items()
        if not any(_overlap(path, change) for change in changes)
    }
    merged = {**kept, **changes}
    return ResourceMapping(
        model,
        record,
        {path: value for path, value in merged.items() if value is not None},
    )


def _overlap(path: str, other: str) -> bool:
    """Tell whether two paths are the same attribute, or one holds the other, whatever their case."""
    path, other = path.lower(), other.lower()
    return path == other or path.startswith(f"{other}.") or other.startswith(f"{path}.")


def _beside(
    record: type[Model], resource_type: str, fields: Mapping[str, str]
) -> dict[str, str]:
    """Return the paths of the attributes the model beside the records holds, among fields, by SCIM path.

    A model without model beside it holds none.
    """
    relation = _relation_beside(record, resource_type)
    if relation is None:
        return {}

    return {
        path: f"{relation.name}__{name}"
        for path, name in fields.items()
        if _field(relation.related_model, name) is not None
    }


def _relation_beside(record: type[Model], resource_type: str) -> Any:
    """Return the reverse one-to-one relation from the records to the model beside them, or None when there is none.

    The model beside them inherits from the abstract model of the resource
    type, which tells it by its scim2_resource_type. Several models beside
    the records raise ImproperlyConfigured.
    """
    relations = [
        relation
        for relation in record._meta.related_objects
        if relation.one_to_one
        and getattr(relation.related_model, "scim2_resource_type", None)
        == resource_type
    ]
    if len(relations) > 1:
        names = ", ".join(sorted(r.related_model._meta.label for r in relations))
        raise ImproperlyConfigured(
            f"Several models are beside the records of {record._meta.label}: {names}"
        )

    return next(iter(relations), None)


def _field(record: type[Model], name: str) -> Any:
    """Return the field of a model, or None when the model has none of that name."""
    try:
        return record._meta.get_field(name)
    except FieldDoesNotExist:
        return None


def _groups(record: type[Model]) -> Any:
    """Return the relation from the users to the groups of django.contrib.auth, or None when the user model has none."""
    field = _field(record, "groups")
    if field is None or field.related_model is not registry.get_model("auth", "Group"):
        return None

    return field


def _identifier(record: type[Model], prefix: str) -> Any:
    """Return the mapping of the id: the primary key, after the prefix."""
    key = record._meta.pk.name
    if not prefix:
        return key

    text: Any = Cast(key, TextField())
    if isinstance(record._meta.pk, UUIDField):
        text = _UUIDText(key)

    expression = Concat(Value(prefix), text, output_field=TextField())
    return Transformed(key, expression=expression, to_field=_without(prefix))


def _password(value: str | None) -> str:
    """Return the hash of a password the validators of the project accept, or an unusable password for no value.

    An empty password is no value, as Django hashes it into a password anyone
    can log in with.
    """
    if not value:
        return make_password(None)

    validate_password(value)
    return make_password(value)


def _without(prefix: str) -> Callable[[str], str]:
    """Return the function removing the prefix from an id, refusing an id without it."""

    def to_field(value: str) -> str:
        if not value.startswith(prefix):
            raise ValueError(f"{value!r} does not start with {prefix!r}")

        return value.removeprefix(prefix)

    return to_field
