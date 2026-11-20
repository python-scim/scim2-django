"""The models of an intranet, before it serves SCIM."""

import uuid

from django.db import models


class Account(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    external_id = models.TextField(null=True)
    login = models.TextField(unique=True)
    first_name = models.TextField(blank=True)
    last_name = models.TextField(blank=True)
    employee_number = models.TextField(blank=True)
    active = models.BooleanField(null=True, default=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    revision = models.PositiveIntegerField(default=1)


class EmailAddress(models.Model):
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="emails"
    )
    address = models.TextField()
    kind = models.TextField(blank=True)
    preferred = models.BooleanField(null=True)

    class Meta:
        ordering = ["id"]


class Team(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    external_id = models.TextField(null=True)
    name = models.TextField()
    members = models.ManyToManyField(Account, related_name="teams", blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    revision = models.PositiveIntegerField(default=1)
