import pytest
from django.conf import settings
from django.db import connections


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--database",
        choices=("sqlite", "postgresql"),
        default="sqlite",
        help="The database of the tests. pytest-postgresql starts a PostgreSQL "
        "server, with the pg_ctl found in the PATH.",
    )


@pytest.fixture(scope="session")
def django_db_modify_db_settings(request: pytest.FixtureRequest) -> None:
    """Point the default database to a PostgreSQL server, with --database=postgresql.

    Django already opened the SQLite database of the settings, so its
    connection is replaced. The test database then gets the suffix of the
    tox environment or of the xdist worker, as pytest-django gives it.
    """
    if request.config.getoption("database") == "postgresql":
        server = request.getfixturevalue("postgresql_proc")
        settings.DATABASES["default"] = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "scim2_django",
            "HOST": server.host,
            "PORT": server.port,
            "USER": server.user,
            "PASSWORD": server.password,
        }
        connections.close_all()
        connections.settings = connections.configure_settings(settings.DATABASES)
        del connections["default"]

    request.getfixturevalue("django_db_modify_db_settings_parallel_suffix")
