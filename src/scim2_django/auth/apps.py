from django.apps import AppConfig

from .signals import connect


class AuthConfig(AppConfig):
    """The dating of the users and the groups of django.contrib.auth in the models beside them, when they change outside SCIM."""

    name = "scim2_django.auth"
    label = "scim2_auth"
    verbose_name = "SCIM for django.contrib.auth"

    def ready(self) -> None:
        connect()
