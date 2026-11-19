from django.apps import AppConfig
from django.core import checks

from .storage.mapping import check_mappings


class Scim2DjangoConfig(AppConfig):
    name = "scim2_django"
    verbose_name = "SCIM"

    def ready(self) -> None:
        """Register the system check of the storage mappings."""
        checks.register(check_mappings)
