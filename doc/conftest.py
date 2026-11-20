import pytest
from django.db.backends.base.base import BaseDatabaseWrapper


@pytest.fixture(autouse=True)
def example_database(db, monkeypatch):
    """Give each page access to the database of the example server.

    The WSGI handler of Django closes the database connection after each
    request. The connection is kept open across the requests of a page, so
    that its test transaction lasts, as the Django test client does.
    """
    monkeypatch.setattr(
        BaseDatabaseWrapper, "close_if_unusable_or_obsolete", lambda self: None
    )
