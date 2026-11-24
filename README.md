<img alt="python-scim logo" src="https://raw.githubusercontent.com/python-scim/.github/refs/heads/main/python-scim.svg" height="72" align="right">

# scim2-django

Serve a SCIM 2.0 server from a [Django](https://www.djangoproject.com/) project, as
[RFC7643](https://datatracker.ietf.org/doc/html/rfc7643) and
[RFC7644](https://datatracker.ietf.org/doc/html/rfc7644) define it. It is built upon
[scim2-server](https://scim2-server.readthedocs.io/) and
[scim2-models](https://scim2-models.readthedocs.io/). It stores the resources in the models of
your project.

## Features

- Storage in the models of your project, with filters, sorts and pages run in the database,
  on SQLite, PostgreSQL and MariaDB
- `manage.py check` checks the mappings between SCIM attributes and model fields
- The users and the groups of `django.contrib.auth` served without any table, and a model beside
  them for `externalId`, `meta.lastModified` and the attributes of your project
- Resource endpoints: `POST`, `GET`, `PUT`, `PATCH` and `DELETE`, for every resource type
- Search: `GET` and `POST /.search`, per resource type and at the server root
- Discovery endpoints: `/ServiceProviderConfig`, `/ResourceTypes` and `/Schemas`
- Bulk operations: `POST /Bulk`, with `bulkId` references, `failOnErrors`, `maxOperations` and
  `maxPayloadSize`
- `/Me`, once your project tells which resource belongs to the authenticated client
- Conditional requests with ETags: `If-Match` and `If-None-Match`
- SCIM error payloads for every failure
- Several SCIM servers in one project, each with its own URL namespace

## Example

```python
# urls.py
from django.urls import path
from scim2_models import ResourceType, ScimProvider, User

from scim2_django import ScimSite
from scim2_django.storage import DjangoStorage, ResourceMapping

from accounts.models import Account

USERS = ResourceMapping(
    User,
    Account,
    {
        "id": "id",
        "userName": "login",
        "meta.created": "created_at",
        "meta.lastModified": "updated_at",
    },
    version="revision",
)


def authenticate(request): ...


provider = ScimProvider.from_discovery(
    USERS.schemas(), [ResourceType.from_resource(User)]
)
storage = DjangoStorage({"User": USERS}, provider)
site = ScimSite(storage, provider, authenticate=authenticate)

urlpatterns = [
    path("scim/v2/", site.urls),
]
```

## Installation

```shell
pip install scim2-django
```

## Documentation

- [Overview](https://scim2-django.readthedocs.io/en/latest/overview.html) builds a SCIM server
  over the models of an existing project.
- [How-to guides](https://scim2-django.readthedocs.io/en/latest/how-to/index.html) cover
  authentication, URLs, reverse proxies, request sizes and transactions.
- [Reference](https://scim2-django.readthedocs.io/en/latest/reference.html) lists the public API
  and the system checks.

## What's SCIM anyway?

SCIM stands for System for Cross-domain Identity Management, and it is a provisioning protocol.
Provisioning is the action of managing a set of resources across different services, usually
users and groups. SCIM is often used between Identity Providers and applications, alongside
standards like OAuth2 and OpenID Connect. It synchronizes the creation, modification and
deletion of users and groups between applications.

## Getting help

Questions and bug reports go to the
[issue tracker](https://github.com/python-scim/scim2-django/issues).

## Contributing

The [contribution page](https://scim2-django.readthedocs.io/en/latest/contributing.html)
describes how to run the tests, the style checks and the documentation build.

## License

scim2-django is released under the Apache-2.0 license.

scim2-django belongs in a collection of SCIM tools developed by [Yaal Coop](https://yaal.coop),
with [scim2-models](https://github.com/python-scim/scim2-models),
[scim2-client](https://github.com/python-scim/scim2-client),
[scim2-server](https://github.com/python-scim/scim2-server),
[scim2-tester](https://github.com/python-scim/scim2-tester) and
[scim2-cli](https://github.com/python-scim/scim2-cli).
