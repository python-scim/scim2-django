import pytest
from asgiref.sync import async_to_sync
from django.core.exceptions import BadRequest
from django.core.exceptions import PermissionDenied
from django.core.exceptions import SuspiciousOperation
from django.http import Http404
from django.test import AsyncClient
from django.test import Client
from django.urls import clear_script_prefix
from django.urls import include
from django.urls import path
from django.urls import reverse
from scim2_models import AuthenticationScheme
from scim2_models import Bulk
from scim2_models import ETag
from scim2_models import Filter
from scim2_models import Pagination
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_models import ServiceProviderConfig
from scim2_models import UnauthorizedException
from scim2_models import User
from scim2_server.memory import InMemoryStorage
from scim2_server.service import ScimService

from scim2_django import ScimSite

SCIM_JSON = "application/scim+json"
USER = {"schemas": [User.__schema__], "userName": "bjensen"}
BULK_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:BulkRequest"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
BEARER = AuthenticationScheme(
    type=AuthenticationScheme.Type.oauthbearertoken,
    name="OAuth Bearer Token",
    description="Authentication with an OAuth bearer token",
)


def accept_anyone(request):
    return None


def create_provider(config: ServiceProviderConfig | None = None) -> ScimProvider:
    return ScimProvider(
        models=[User],
        resource_types=[ResourceType.from_resource(User)],
        config=config,
    )


def create_site(**kwargs) -> ScimSite:
    kwargs.setdefault("provider", create_provider())
    kwargs.setdefault("authenticate", accept_anyone)
    return ScimSite(kwargs.pop("storage", InMemoryStorage()), **kwargs)


def bulk_payload(size: int) -> dict:
    """Return a bulk request whose JSON body is at least ``size`` bytes long."""
    return {
        "schemas": [BULK_SCHEMA],
        "Operations": [
            {"method": "DELETE", "path": f"/Users/{'x' * size}"},
        ],
    }


@pytest.fixture
def client(serve) -> Client:
    return serve(path("scim/v2/", create_site().urls))


def test_requires_at_least_one_resource_type():
    with pytest.raises(ValueError, match="at least one resource type"):
        ScimSite(InMemoryStorage(), ScimProvider())


def test_endpoints_are_served_under_the_prefix_of_the_include(serve):
    client = serve(path("api/scim/", create_site().urls))

    created = client.post("/api/scim/Users", USER, content_type=SCIM_JSON)
    assert created.status_code == 201
    assert created.json()["meta"]["location"] == (
        f"http://testserver/api/scim/Users/{created.json()['id']}"
    )
    assert client.get("/api/scim/Users").json()["totalResults"] == 1


def test_endpoints_can_be_served_at_the_project_root(serve):
    client = serve(path("", create_site().urls))

    created = client.post("/Users", USER, content_type=SCIM_JSON)
    assert created.json()["meta"]["location"] == (
        f"http://testserver/Users/{created.json()['id']}"
    )
    assert client.get("/").json()["totalResults"] == 1


def test_locations_follow_the_host_the_scheme_and_the_script_name(serve, settings):
    settings.ALLOWED_HOSTS = ["scim.example.org"]
    client = serve(path("scim/v2/", create_site().urls))

    try:
        created = client.post(
            "/scim/v2/Users",
            USER,
            content_type=SCIM_JSON,
            headers={"host": "scim.example.org"},
            secure=True,
            SCRIPT_NAME="/app",
        )
    finally:
        clear_script_prefix()

    assert created.headers["Location"] == (
        f"https://scim.example.org/app/scim/v2/Users/{created.json()['id']}"
    )


def test_locations_follow_the_headers_of_a_trusted_proxy(serve, settings):
    settings.ALLOWED_HOSTS = ["scim.example.org"]
    settings.USE_X_FORWARDED_HOST = True
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    client = serve(path("scim/v2/", create_site().urls))

    created = client.post(
        "/scim/v2/Users",
        USER,
        content_type=SCIM_JSON,
        headers={"X-Forwarded-Host": "scim.example.org", "X-Forwarded-Proto": "https"},
    )

    assert created.headers["Location"] == (
        f"https://scim.example.org/scim/v2/Users/{created.json()['id']}"
    )


def test_several_sites_serve_one_project(serve):
    client = serve(
        path("a/scim/v2/", create_site(name="tenant_a").urls),
        path("b/scim/v2/", create_site(name="tenant_b").urls),
    )

    created = client.post("/a/scim/v2/Users", USER, content_type=SCIM_JSON)
    assert created.json()["meta"]["location"] == (
        f"http://testserver/a/scim/v2/Users/{created.json()['id']}"
    )
    assert client.get("/a/scim/v2/Users").json()["totalResults"] == 1
    assert client.get("/b/scim/v2/Users").json()["totalResults"] == 0


def test_a_site_can_be_included_in_another_namespace(serve):
    api = ([path("scim/v2/", create_site().urls)], "api")
    client = serve(path("api/", include(api, namespace="api")))

    created = client.post("/api/scim/v2/Users", USER, content_type=SCIM_JSON)
    assert created.json()["meta"]["location"] == (
        f"http://testserver/api/scim/v2/Users/{created.json()['id']}"
    )


def test_urls_are_named_after_the_routes(serve):
    serve(path("scim/v2/", create_site(name="tenant").urls))

    assert reverse(
        "tenant:query", kwargs={"endpoint": "Users", "resource_id": "42"}
    ) == ("/scim/v2/Users/42")
    assert reverse("scim:service_provider_config", current_app="tenant") == (
        "/scim/v2/ServiceProviderConfig"
    )
    assert reverse("tenant:search_with_body", kwargs={"endpoint": "Users"}) == (
        "/scim/v2/Users/.search"
    )


def test_urls_of_several_sites_are_reversed_by_name(serve):
    serve(
        path("a/scim/v2/", create_site(name="tenant_a").urls),
        path("b/scim/v2/", create_site(name="tenant_b").urls),
    )

    assert reverse(
        "tenant_a:query", kwargs={"endpoint": "Users", "resource_id": "2819c223"}
    ) == ("/a/scim/v2/Users/2819c223")
    assert reverse("tenant_a:service_provider_config") == (
        "/a/scim/v2/ServiceProviderConfig"
    )
    assert reverse("scim:service_provider_config", current_app="tenant_a") == (
        "/a/scim/v2/ServiceProviderConfig"
    )
    assert reverse("scim:service_provider_config") == (
        "/b/scim/v2/ServiceProviderConfig"
    )


def test_the_routes_of_one_path_share_their_url(client):
    assert reverse("scim:search", kwargs={"endpoint": "Users"}) == reverse(
        "scim:create", kwargs={"endpoint": "Users"}
    )
    assert reverse("scim:me_query") == reverse("scim:me_create") == "/scim/v2/Me"


def test_each_method_of_a_path_reaches_its_operation(client):
    created = client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)
    location = created.headers["Location"]

    replaced = client.put(
        location, {**USER, "displayName": "Babs"}, content_type=SCIM_JSON
    )
    assert replaced.json()["displayName"] == "Babs"

    searched = client.post(
        "/scim/v2/Users/.search",
        {"schemas": ["urn:ietf:params:scim:api:messages:2.0:SearchRequest"]},
        content_type=SCIM_JSON,
    )
    assert searched.json()["totalResults"] == 1


def test_unknown_path_answers_a_scim_404(client):
    response = client.get("/scim/v2/Users/42/unknown")

    assert response.status_code == 404
    assert response.headers["Content-Type"] == SCIM_JSON
    assert response.json()["schemas"] == [ERROR_SCHEMA]


def test_path_with_a_trailing_slash_is_served(client):
    """The SCIM routes ignore trailing slashes, and Django does not redirect."""
    response = client.get("/scim/v2/Users/")

    assert response.status_code == 200
    assert response.json()["totalResults"] == 0


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("put", "/scim/v2/Users", "GET, POST"),
        ("delete", "/scim/v2/", "GET"),
        ("get", "/scim/v2/Bulk", "POST"),
        ("put", "/scim/v2/ResourceTypes/User", "GET"),
    ],
)
def test_unsupported_method_answers_a_scim_405(client, method, path, allowed):
    response = getattr(client, method)(path)

    assert response.status_code == 405
    assert response.headers["Allow"] == allowed
    assert response.headers["Content-Type"] == SCIM_JSON


def test_responses_keep_the_status_headers_and_body_of_the_service(serve):
    site = create_site(
        provider=create_provider(ServiceProviderConfig(etag=ETag(supported=True)))
    )
    client = serve(path("scim/v2/", site.urls))

    created = client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)
    assert created.status_code == 201
    assert created.headers["Content-Type"] == SCIM_JSON
    assert created.headers["ETag"] == created.json()["meta"]["version"]
    location = created.headers["Location"]

    not_modified = client.get(
        location, headers={"If-None-Match": created.headers["ETag"]}
    )
    assert not_modified.status_code == 304
    assert "Content-Type" not in not_modified.headers

    deleted = client.delete(location)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert "Content-Type" not in deleted.headers


def test_query_string_is_passed_to_the_service(serve):
    config = ServiceProviderConfig(filter=Filter(supported=True, max_results=10))
    client = serve(path("scim/v2/", create_site(provider=create_provider(config)).urls))
    client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)
    client.post(
        "/scim/v2/Users", {**USER, "userName": "jsmith"}, content_type=SCIM_JSON
    )

    response = client.get("/scim/v2/Users", {"filter": 'userName eq "jsmith"'})
    assert [user["userName"] for user in response.json()["Resources"]] == ["jsmith"]


def test_writes_need_no_csrf_token_nor_login(client):
    """The views are exempt from CsrfViewMiddleware and LoginRequiredMiddleware."""
    response = client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)

    assert response.status_code == 201


def test_requests_are_served_under_asgi(client):
    async_client = AsyncClient()

    created = async_to_sync(async_client.post)(
        "/scim/v2/Users", USER, content_type=SCIM_JSON
    )
    assert created.status_code == 201

    bulk = async_to_sync(async_client.post)(
        "/scim/v2/Bulk", bulk_payload(10), content_type=SCIM_JSON
    )
    assert bulk.status_code == 501


def test_bulk_body_beyond_max_payload_size_is_refused(serve):
    config = ServiceProviderConfig(
        bulk=Bulk(supported=True, max_operations=10, max_payload_size=100)
    )
    client = serve(path("scim/v2/", create_site(provider=create_provider(config)).urls))

    response = client.post("/scim/v2/Bulk", bulk_payload(200), content_type=SCIM_JSON)
    assert response.status_code == 413
    assert response.headers["Content-Type"] == SCIM_JSON


def test_bulk_body_within_max_payload_size_is_served(serve):
    config = ServiceProviderConfig(
        bulk=Bulk(supported=True, max_operations=10, max_payload_size=1000)
    )
    client = serve(path("scim/v2/", create_site(provider=create_provider(config)).urls))

    response = client.post("/scim/v2/Bulk", bulk_payload(10), content_type=SCIM_JSON)
    assert response.status_code == 200
    assert response.json()["Operations"][0]["status"] == "404"


def test_unsupported_bulk_is_refused_whatever_its_max_payload_size(serve):
    config = ServiceProviderConfig(
        bulk=Bulk(supported=False, max_operations=0, max_payload_size=0)
    )
    client = serve(path("scim/v2/", create_site(provider=create_provider(config)).urls))

    response = client.post("/scim/v2/Bulk", bulk_payload(10), content_type=SCIM_JSON)
    assert response.status_code == 501


def test_stricter_project_limit_is_kept_for_bulk_requests(serve, settings):
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 50
    config = ServiceProviderConfig(
        bulk=Bulk(supported=True, max_operations=10, max_payload_size=1000)
    )
    client = serve(path("scim/v2/", create_site(provider=create_provider(config)).urls))

    response = client.post("/scim/v2/Bulk", bulk_payload(100), content_type=SCIM_JSON)
    assert response.status_code == 413
    assert response.headers["Content-Type"] == SCIM_JSON


def test_bulk_requests_are_not_limited_without_project_limit(serve, settings):
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = None
    config = ServiceProviderConfig(
        bulk=Bulk(supported=True, max_operations=10, max_payload_size=1000)
    )
    client = serve(path("scim/v2/", create_site(provider=create_provider(config)).urls))

    response = client.post("/scim/v2/Bulk", bulk_payload(100), content_type=SCIM_JSON)
    assert response.status_code == 200


def test_project_limit_applies_to_the_other_requests(client, settings):
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 10

    response = client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)
    assert response.status_code == 413
    assert response.headers["Content-Type"] == SCIM_JSON
    assert response.json()["schemas"] == [ERROR_SCHEMA]


def test_project_limit_applies_under_asgi(client, settings):
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 10

    response = async_to_sync(AsyncClient().post)(
        "/scim/v2/Users", USER, content_type=SCIM_JSON
    )
    assert response.status_code == 413
    assert response.headers["Content-Type"] == SCIM_JSON


def test_max_payload_size_only_limits_bulk_requests(serve):
    config = ServiceProviderConfig(
        bulk=Bulk(supported=True, max_operations=10, max_payload_size=10)
    )
    client = serve(path("scim/v2/", create_site(provider=create_provider(config)).urls))

    response = client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)
    assert response.status_code == 201


def test_requests_are_refused_without_authentication_function(serve):
    """Requests get a 401 with a WWW-Authenticate header.

    RFC7644 §2: "a SCIM service provider SHALL indicate supported HTTP
    authentication schemes via the "WWW-Authenticate" header."
    """
    config = ServiceProviderConfig(authentication_schemes=[BEARER])
    site = ScimSite(InMemoryStorage(), create_provider(config))
    client = serve(path("scim/v2/", site.urls))

    for url in ("/scim/v2/Users", "/scim/v2/Users/", "/scim/v2/Schemas"):
        response = client.get(url)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")
        assert response.headers["Content-Type"] == SCIM_JSON


def test_service_provider_config_needs_no_authentication(serve):
    """RFC7643 §5: clients read the authentication schemes before they authenticate."""
    site = ScimSite(InMemoryStorage(), create_provider())
    client = serve(path("scim/v2/", site.urls))

    response = client.get("/scim/v2/ServiceProviderConfig")
    assert response.status_code == 200


def test_authentication_refusing_a_request_answers_a_scim_401(serve):
    def refuse(request):
        raise UnauthorizedException(detail="Missing token")

    config = ServiceProviderConfig(authentication_schemes=[BEARER])
    site = create_site(provider=create_provider(config), authenticate=refuse)
    client = serve(path("scim/v2/", site.urls))

    response = client.get("/scim/v2/Users")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert response.json()["detail"] == "Missing token"


@pytest.mark.parametrize(
    ("exception", "status", "detail"),
    [
        (PermissionDenied("Read only token"), 403, "Read only token"),
        (Http404("No such tenant"), 404, "No such tenant"),
        (BadRequest("Malformed token"), 400, "Malformed token"),
        (SuspiciousOperation("Secret detail"), 400, "Bad request"),
    ],
)
def test_django_exceptions_raised_by_authentication_answer_scim_errors(
    serve, exception, status, detail
):
    def refuse(request):
        raise exception

    client = serve(path("scim/v2/", create_site(authenticate=refuse).urls))

    response = client.get("/scim/v2/Users")
    assert response.status_code == status
    assert response.headers["Content-Type"] == SCIM_JSON
    assert response.json()["detail"] == detail


@pytest.mark.parametrize(
    ("exception", "detail"),
    [
        (
            PermissionDenied(),
            "Operation is not permitted based on the supplied authorization",
        ),
        (Http404(), "Specified resource or endpoint does not exist"),
    ],
)
def test_django_exceptions_without_message_get_the_scim_detail(
    serve, exception, detail
):
    def refuse(request):
        raise exception

    client = serve(path("scim/v2/", create_site(authenticate=refuse).urls))

    response = client.get("/scim/v2/Users")
    assert response.json()["detail"] == detail


def test_authentication_may_read_the_body(serve):
    """An authentication checking a signature reads the body before the server does."""
    bodies = []

    def read_body(request):
        bodies.append(request.body)

    config = ServiceProviderConfig(
        bulk=Bulk(supported=True, max_operations=10, max_payload_size=1000)
    )
    site = create_site(provider=create_provider(config), authenticate=read_body)
    client = serve(path("scim/v2/", site.urls))

    assert (
        client.post("/scim/v2/Users", USER, content_type=SCIM_JSON).status_code == 201
    )
    bulk = client.post("/scim/v2/Bulk", bulk_payload(10), content_type=SCIM_JSON)
    assert bulk.status_code == 200
    assert len(bodies) == 2


def test_subject_is_passed_to_the_service(serve):
    class MeService(ScimService):
        def me_target(self, request):
            return self.get_resource_type("User"), request.subject

    provider = create_provider()
    site = create_site(
        provider=provider,
        service=MeService(provider),
        authenticate=lambda request: request.headers.get("X-User"),
    )
    client = serve(path("scim/v2/", site.urls))
    created = client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)

    response = client.get("/scim/v2/Me", headers={"X-User": created.json()["id"]})
    assert response.status_code == 200
    assert response.json()["userName"] == "bjensen"
    assert response.headers["Location"] == created.headers["Location"]


def test_steps_of_the_service_can_be_overridden(serve):
    class PublicService(ScimService):
        def resource_location(self, base_url, resource_type, resource_id):
            return f"https://scim.example.org/v2/Users/{resource_id}"

    provider = create_provider()
    site = create_site(provider=provider, service=PublicService(provider))
    client = serve(path("scim/v2/", site.urls))

    created = client.post("/scim/v2/Users", USER, content_type=SCIM_JSON)
    assert created.json()["meta"]["location"] == (
        f"https://scim.example.org/v2/Users/{created.json()['id']}"
    )


def test_cursors_are_sealed_with_the_secret_key(serve):
    """A cursor from one process is accepted by another one with the same secret key."""
    storage = InMemoryStorage()
    config = ServiceProviderConfig(pagination=Pagination(cursor=True, index=True))
    client = serve(
        path(
            "a/",
            create_site(
                storage=storage, provider=create_provider(config), name="a"
            ).urls,
        ),
        path(
            "b/",
            create_site(
                storage=storage, provider=create_provider(config), name="b"
            ).urls,
        ),
    )
    for user_name in ("bjensen", "jsmith"):
        client.post("/a/Users", {**USER, "userName": user_name}, content_type=SCIM_JSON)

    page = client.get("/a/Users", {"cursor": "", "count": 1})
    next_page = client.get(
        "/b/Users", {"cursor": page.json()["nextCursor"], "count": 1}
    )

    assert next_page.status_code == 200
    user_names = {page.json()["Resources"][0]["userName"]}
    user_names.add(next_page.json()["Resources"][0]["userName"])
    assert user_names == {"bjensen", "jsmith"}


def test_given_service_keeps_its_own_secret(serve):
    config = ServiceProviderConfig(pagination=Pagination(cursor=True, index=True))
    provider = create_provider(config)
    site = create_site(provider=provider, service=ScimService(provider, secret="s"))
    client = serve(path("scim/v2/", site.urls))

    response = client.get("/scim/v2/Users", {"cursor": "", "count": 1})
    assert response.status_code == 200


def test_unexpected_exceptions_reach_django(serve):
    class BrokenStorage(InMemoryStorage):
        def search(self, resource_types, search_request, *, position=None):
            raise RuntimeError("database is down")

    client = serve(path("scim/v2/", create_site(storage=BrokenStorage()).urls))

    with pytest.raises(RuntimeError, match="database is down"):
        client.get("/scim/v2/Users")
