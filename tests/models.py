import uuid

from django.db import models
from django.db.models.functions import Lower


class CodeField(models.TextField):
    """A text field converting the codes it reads, as a field decrypting its values would."""

    def from_db_value(self, value, expression, connection):
        return None if value is None else value.upper()


class LooseTextField(models.Field):
    """A field built on Field, which Django stores as text."""

    def get_internal_type(self):
        return "TextField"


class UserRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    external_id = models.TextField(null=True)
    user_name = models.TextField(unique=True)
    display_name = models.TextField(null=True)
    title = models.TextField(null=True)
    nick_name = models.TextField(null=True)
    user_type = models.TextField(null=True, default="Employee")
    given_name = models.TextField(null=True)
    family_name = models.TextField(null=True)
    active = models.BooleanField(null=True)
    password = models.TextField(null=True)
    employee_number = models.TextField(null=True)
    preferred_language = models.TextField()
    badge_number = models.IntegerField(null=True)
    score = models.FloatField(null=True)
    code = CodeField(null=True)
    manager = models.ForeignKey(
        "self", null=True, on_delete=models.SET_NULL, related_name="reports"
    )
    lower_name = models.GeneratedField(
        expression=Lower("user_name"),
        output_field=models.TextField(),
        db_persist=True,
    )
    created = models.DateTimeField(null=True)
    last_modified = models.DateTimeField(null=True)
    version = models.PositiveIntegerField(default=1)


class UserScim(models.Model):
    """A table beside the users, holding what SCIM adds to them."""

    user = models.OneToOneField(
        UserRecord, on_delete=models.CASCADE, primary_key=True, related_name="scim"
    )
    external_id = models.TextField(null=True)
    division = models.TextField(blank=True)
    level = models.IntegerField(default=0)


class UserCard(models.Model):
    """A table beside the users that cannot be created without a number."""

    user = models.OneToOneField(
        UserRecord, on_delete=models.CASCADE, related_name="card"
    )
    number = models.IntegerField()
    note = models.TextField(null=True)


class UserBadge(models.Model):
    """A table beside the users, queried by another name than its accessor."""

    user = models.OneToOneField(
        UserRecord,
        on_delete=models.CASCADE,
        related_name="badge",
        related_query_name="badge_record",
    )
    label = models.TextField(null=True)


class EmailRecord(models.Model):
    user = models.ForeignKey(
        UserRecord, on_delete=models.CASCADE, related_name="emails"
    )
    value = models.TextField(null=True)
    type = models.TextField(null=True)
    primary = models.BooleanField(null=True)

    class Meta:
        ordering = ["id"]


class DescendingManager(models.Manager):
    """A manager listing the records by descending value."""

    def get_queryset(self):
        return super().get_queryset().order_by("-value")


class PhoneRecord(models.Model):
    """Entries ordered by their manager, not by their model."""

    user = models.ForeignKey(
        UserRecord, on_delete=models.CASCADE, related_name="phones"
    )
    value = models.TextField()

    objects = DescendingManager()


class CertificateRecord(models.Model):
    user = models.ForeignKey(
        UserRecord, on_delete=models.CASCADE, related_name="certificates"
    )
    value = models.BinaryField()
    display = models.TextField(null=True)
    primary = models.BooleanField(default=False)


class GroupRecord(models.Model):
    external_id = models.TextField(null=True)
    display_name = models.TextField(null=True)
    members = models.ManyToManyField(UserRecord, related_name="groups")
    subgroups = models.ManyToManyField(
        "self", symmetrical=False, related_name="parents"
    )
    created = models.DateTimeField(null=True)
    last_modified = models.DateTimeField(null=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["display_name"]


class BadgeRecord(models.Model):
    """A record outside SCIM, whose user cannot be deleted while it exists."""

    user = models.ForeignKey(UserRecord, on_delete=models.PROTECT)


class Person(models.Model):
    """A model for the mappings the checks refuse."""

    id = models.TextField(primary_key=True)
    user_name = models.TextField()
    serial = models.TextField(unique=True, null=True)
    data = models.JSONField(null=True)
    code = CodeField(null=True)
    status = models.TextField(default="new")
    note = LooseTextField(null=True)
    enabled = models.BooleanField()
    verified = models.BooleanField(db_default=False)
    touched = models.DateTimeField(auto_now=True)
    mentor = models.ForeignKey(UserRecord, on_delete=models.CASCADE)
    label = models.TextField(null=True, choices=[("Mr", "Mister"), ("Ms", "Miss")])
    created = models.DateTimeField()
    last_modified = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)


class Address(models.Model):
    person = models.ForeignKey(
        Person, on_delete=models.CASCADE, related_name="addresses"
    )
    value = models.TextField(null=True)


class Note(models.Model):
    """Entries that outlive their person, which Many cannot replace."""

    person = models.ForeignKey(
        Person, null=True, on_delete=models.SET_NULL, related_name="notes"
    )
    value = models.TextField(null=True)


class TaggedUser(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user_name = models.TextField(unique=True)
    created = models.DateTimeField()
    last_modified = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)


class Tag(models.Model):
    user = models.ForeignKey(TaggedUser, on_delete=models.CASCADE, related_name="tags")
    value = models.TextField()
    note = models.TextField(null=True)

    class Meta:
        ordering = ["id"]
