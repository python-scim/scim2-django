from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import BadRequest
from django.core.exceptions import PermissionDenied
from django.core.exceptions import RequestDataTooBig
from django.core.exceptions import SuspiciousOperation
from django.http import Http404
from django.http import HttpRequest
from django.http import HttpResponse
from django.urls import URLPattern
from django.urls import get_script_prefix
from django.urls import path
from django.urls import re_path
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from scim2_models import ForbiddenException
from scim2_models import NotFoundException
from scim2_models import PayloadTooLargeException
from scim2_models import SCIMException
from scim2_models import ScimProvider
from scim2_models import UnauthorizedException
from scim2_server.handler import ScimHandler
from scim2_server.requests import ScimRequest
from scim2_server.responses import ScimResponse
from scim2_server.routing import ROUTES
from scim2_server.routing import Operation
from scim2_server.routing import Route
from scim2_server.service import ScimService
from scim2_server.storage import ScimStorage

APP_NAMESPACE = "scim"
"""The application namespace of the URLs of every :class:`ScimSite`."""

View = Callable[..., HttpResponse]


class ScimSite:
    """A SCIM 2.0 server backed by a :class:`~scim2_server.storage.ScimStorage`.

    The :class:`~scim2_models.ScimProvider` describes the service: the
    resource types it serves, the configuration it announces, and the policy
    the server applies when it reads and writes payloads.

    Include its :attr:`urls` in the URLconf of the project::

        site = ScimSite(storage, ScimProvider(models=[User]), authenticate=authenticate)

        urlpatterns = [
            path("scim/v2/", site.urls),
        ]

    :param storage: The storage the server reads and writes resources in.
    :param provider: The service description. It must declare at least one
        resource type.
    :param service: The service serving the requests. By default, a
        :class:`~scim2_server.service.ScimService` upon ``provider``, with the
        :setting:`SECRET_KEY` of the project as the secret of the cursors.
        Pass a subclass of :class:`~scim2_server.service.ScimService` to change
        one of its steps, such as the URL of the resources.
    :param name: The instance namespace of the URLs. Give each instance its own
        name to serve several SCIM servers from one project.
    :param authenticate: The function authenticating a request. It returns the
        subject of the request, and raises
        :class:`~scim2_models.UnauthorizedException` to refuse it. Without it,
        every request gets a ``401``, except the ones on
        ``/ServiceProviderConfig``.
    """

    def __init__(
        self,
        storage: ScimStorage,
        provider: ScimProvider,
        *,
        service: ScimService | None = None,
        name: str = APP_NAMESPACE,
        authenticate: Callable[[HttpRequest], Any] | None = None,
    ) -> None:
        if not provider.resource_types:
            raise ValueError("ScimSite requires at least one resource type")

        self.storage = storage
        self.provider = provider
        self.service = service or ScimService(provider, secret=settings.SECRET_KEY)
        self.handler = ScimHandler(self.service, self.storage)
        self.name = name
        self._authenticate = authenticate

    @property
    def urls(self) -> tuple[list[URLPattern], str, str]:
        """The URL patterns of the SCIM endpoints, to pass to :func:`~django.urls.path`.

        The application namespace is ``scim``, and the instance namespace is
        the ``name`` of the site.
        """
        return self.get_urls(), APP_NAMESPACE, self.name

    def get_urls(self) -> list[URLPattern]:
        """Return the URL patterns of the SCIM endpoints.

        There is one pattern per route of :data:`~scim2_server.routing.ROUTES`,
        named after the route, such as ``query``, whose arguments are
        ``endpoint`` and ``resource_id``. A last ``fallback`` pattern answers
        the other requests with a SCIM error.
        """
        views: dict[str, View] = {}
        patterns = []
        for route in ROUTES:
            rule = django_rule(route)
            if rule not in views:
                operations = {
                    other.method: other.operation
                    for other in ROUTES
                    if django_rule(other) == rule
                }
                views[rule] = self._make_view(operations)
            patterns.append(path(rule, views[rule], name=route.name))
        patterns.append(re_path(r"^.*$", self._make_view({}), name="fallback"))
        return patterns

    def _make_view(self, operations: dict[str, Operation]) -> View:
        # Django matches a request on its path only, whatever its method.
        # A view serves every route of a path, and lets the handler answer
        # the methods none of them supports.
        @csrf_exempt
        @login_not_required
        def view(request: HttpRequest, **_kwargs: str) -> HttpResponse:
            return self.serve(request, operations.get(request.method or ""))

        return view

    def serve(self, request: HttpRequest, operation: Operation | None) -> HttpResponse:
        """Serve a request with the handler method of an operation.

        Without an operation, the handler routes the request itself.
        """
        try:
            scim_request = self.read_request(request)
            if operation is not Operation.service_provider_config:
                scim_request.subject = self.authenticate(request)
            scim_request.body = self.read_body(request, scim_request)
            if operation is None:
                scim_response = self.handler.handle(scim_request)
            else:
                scim_response = getattr(self.handler, operation.value)(scim_request)
        except (
            SCIMException,
            Http404,
            PermissionDenied,
            BadRequest,
            SuspiciousOperation,
        ) as exception:
            scim_response = self.service.error_response(scim_exception(exception))
        return to_response(scim_response)

    def authenticate(self, request: HttpRequest) -> Any:
        """Return the subject of a request, as the ``authenticate`` function gives it.

        :raises ~scim2_models.UnauthorizedException: When the site has no
            ``authenticate`` function.
        """
        if self._authenticate is None:
            raise UnauthorizedException(detail="Authentication required")
        return self._authenticate(request)

    def read_request(self, request: HttpRequest) -> ScimRequest:
        """Return the :class:`~scim2_server.requests.ScimRequest` of a Django request, without its body."""
        assert request.resolver_match is not None
        root = reverse(f"{request.resolver_match.namespace}:search_root")
        # path_info, unlike the URLs reverse() builds, holds no script prefix.
        root_info = "/" + root.removeprefix(get_script_prefix())
        scim_path = request.path_info.removeprefix(root_info.rstrip("/"))
        base_path = request.path.removesuffix(scim_path) or "/"
        return ScimRequest(
            method=request.method or "",
            base_url=request.build_absolute_uri(base_path).rstrip("/"),
            path=scim_path,
            query=request.GET.dict(),
            headers=list(request.headers.items()),
        )

    def read_body(self, request: HttpRequest, scim_request: ScimRequest) -> bytes:
        """Read the body of a request, up to one byte more than the service accepts.

        The service answers a larger body with a 413. The
        :setting:`DATA_UPLOAD_MAX_MEMORY_SIZE` of the project still applies.

        :raises RequestDataTooBig: When the body is
            larger than :setting:`DATA_UPLOAD_MAX_MEMORY_SIZE`.
        """
        limit = self.service.max_body_size(scim_request)
        if limit is None:
            return request.body
        body = request.read(limit + 1)
        project_limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        if project_limit is not None and len(body) > project_limit:
            raise RequestDataTooBig(
                "Request body exceeded settings.DATA_UPLOAD_MAX_MEMORY_SIZE."
            )
        return body


def scim_exception(exception: Exception) -> SCIMException:
    """Return the SCIM exception standing for an exception of Django or of SCIM."""
    if isinstance(exception, SCIMException):
        return exception
    if isinstance(exception, Http404):
        return NotFoundException(detail=str(exception) or None)
    if isinstance(exception, PermissionDenied):
        return ForbiddenException(detail=str(exception) or None)
    if isinstance(exception, RequestDataTooBig):
        return PayloadTooLargeException(detail=str(exception))
    if isinstance(exception, BadRequest):
        return SCIMException(detail=str(exception) or None)
    # The message of a suspicious operation may reveal the configuration.
    return SCIMException(detail="Bad request")


def to_response(scim_response: ScimResponse) -> HttpResponse:
    """Return the Django response of a :class:`~scim2_server.responses.ScimResponse`."""
    if scim_response.body is None:
        response = HttpResponse(
            status=scim_response.status, headers=scim_response.headers
        )
        del response["Content-Type"]
        return response
    return HttpResponse(
        json.dumps(scim_response.body),
        status=scim_response.status,
        headers=scim_response.headers,
    )


def django_rule(route: Route) -> str:
    """Return the Django route of a route of :data:`~scim2_server.routing.ROUTES`."""
    return (
        route.pattern.removeprefix("/")
        .replace("{endpoint}", "<endpoint>")
        .replace("{resource_id}", "<resource_id>")
    )
