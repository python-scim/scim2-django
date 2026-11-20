from scim2_models import User

from scim2_django.storage import ResourceMapping

from .models import Person

mapping = ResourceMapping(
    User,
    Person,
    {"id": "id", "meta.created": "created", "meta.lastModified": "modified"},
    version="version",
)

urlpatterns = []
