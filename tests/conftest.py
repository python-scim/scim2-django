from collections.abc import Callable
from collections.abc import Iterator

import pytest
from django.test import Client
from django.urls import URLPattern
from django.urls import URLResolver

from scim2_django.storage.mapping import _MAPPINGS


class URLconf:
    """A URLconf module built in memory, hashable as Django caches its resolver."""

    def __init__(self, patterns: list[URLPattern | URLResolver]) -> None:
        self.urlpatterns = patterns


def create_client() -> Client:
    """Return a test client checking CSRF tokens, as a browser would need."""
    return Client(enforce_csrf_checks=True)


@pytest.fixture
def client() -> Client:
    return create_client()


@pytest.fixture
def serve(settings) -> Callable[..., Client]:
    """Serve URL patterns as the URLconf of the project, and return a test client."""

    def serve(*patterns: URLPattern | URLResolver) -> Client:
        settings.ROOT_URLCONF = URLconf(list(patterns))
        return create_client()

    return serve


@pytest.fixture(autouse=True)
def forget_the_mappings() -> Iterator[None]:
    """Check, in each test, the mappings this test creates only."""
    _MAPPINGS.clear()
    yield
    _MAPPINGS.clear()
