import getpass
import shutil

import pytest
from django.conf import settings
from django.db import connections
from pytest_mysql import factories

mariadb_proc = factories.mysql_proc(
    mysqld_exec=shutil.which("mariadbd"),
    admin_executable=shutil.which("mariadb-admin"),
    mysqld_safe=shutil.which("mariadbd-safe"),
    install_db=shutil.which("mariadb-install-db"),
    user=getpass.getuser(),
)
"""A MariaDB server, started with the binaries found in the PATH.

It runs as the current user, who connects through the socket of the server.
"""


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--database",
        choices=("sqlite", "postgresql", "mariadb"),
        default="sqlite",
        help="The database of the tests. pytest-postgresql starts a PostgreSQL "
        "server, with the pg_ctl found in the PATH. pytest-mysql starts a "
        "MariaDB server, with the mariadbd found in the PATH.",
    )


@pytest.fixture(scope="session")
def django_db_modify_db_settings(request: pytest.FixtureRequest) -> None:
    """Point the default database to a PostgreSQL or a MariaDB server, with --database.

    Django already opened the SQLite database of the settings, so its
    connection is replaced. The test database then gets the suffix of the
    tox environment or of the xdist worker, as pytest-django gives it.
    """
    database = request.config.getoption("database")
    if database == "postgresql":
        server = request.getfixturevalue("postgresql_proc")
        _replace_default(
            {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "scim2_django",
                "HOST": server.host,
                "PORT": server.port,
                "USER": server.user,
                "PASSWORD": server.password,
            }
        )
    elif database == "mariadb":
        server = request.getfixturevalue("mariadb_proc")
        _replace_default(
            {
                "ENGINE": "django.db.backends.mysql",
                "NAME": "scim2_django",
                "HOST": server.unixsocket,
                "USER": server.user,
                "OPTIONS": {"charset": "utf8mb4"},
                "TEST": {"CHARSET": "utf8mb4"},
            }
        )

    request.getfixturevalue("django_db_modify_db_settings_parallel_suffix")


def _replace_default(database: dict[str, object]) -> None:
    """Replace the default database of the settings, and its connection."""
    settings.DATABASES["default"] = database
    connections.close_all()
    connections.settings = connections.configure_settings(settings.DATABASES)
    del connections["default"]
