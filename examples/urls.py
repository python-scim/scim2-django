from django.urls import path

from examples import authentication
from examples import authorization
from examples import scim

urlpatterns = [
    path("scim/v2/", scim.site.urls),
    path("protected/scim/v2/", authentication.site.urls),
    path("read-only/scim/v2/", authorization.site.urls),
]
