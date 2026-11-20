Overview
========

This tutorial builds a SCIM server over the models of an existing Django project. The project is
an intranet that keeps accounts, their email addresses, and teams. At the end, the server serves
the accounts as SCIM users and the teams as SCIM groups, and passes the checks of
:doc:`scim2-tester <scim2_tester:index>`.

The tutorial is for Python developers whose project stores its data with the Django ORM. It
assumes familiarity with Django models and the URLconf, and with the users and groups of SCIM
(:rfc:`RFC 7643 §4 <7643#section-4>`). It accepts every client. To authenticate them, read
:doc:`how-to/authenticate-the-clients`.

Install scim2-django. It brings Django, :doc:`scim2-server <scim2_server:index>` and
:doc:`scim2-models <scim2_models:index>` along:

.. code-block:: console

   $ pip install scim2-django

Start from the models
---------------------

The intranet already has its models. scim2-django uses them as they are. An account has a login,
a name, an employee number, email addresses and teams:

.. literalinclude:: ../examples/models.py
   :language: python
   :pyobject: Account

The models need a primary key unique across the accounts and the teams, here a UUID. Per
:rfc:`RFC 7643 §3.1 <7643#section-3.1>`, two resources never share an ``id``, even of different
types.

Four more fields serve SCIM, and the models can do without them:

- a field for the identifier the identity provider gives, here ``external_id``;
- a creation date and a modification date, here ``created_at`` and ``updated_at``. Without
  them, the resources have no ``meta.created`` and no ``meta.lastModified``;
- an integer field counting the writes, here ``revision``. The storage increments it on every
  write, and SCIM clients send the version back in their ``If-Match`` headers. Without it, the
  version is a hash of the whole resource. The storage then reads every collection of a
  resource, such as all the members of a group, even when a response leaves them out.

The email addresses and the teams live in models of their own:

.. literalinclude:: ../examples/models.py
   :language: python
   :pyobject: EmailAddress

.. literalinclude:: ../examples/models.py
   :language: python
   :pyobject: Team

Map the accounts to users
-------------------------

A :class:`~scim2_django.storage.ResourceMapping` stores a SCIM model in a Django model. It maps
the path of each SCIM attribute to a field. The following mapping stores the
:class:`~scim2_models.User` resources, with the enterprise extension, in the ``Account`` model:

.. literalinclude:: ../examples/scim.py
   :language: python
   :start-at: USERS =
   :end-before: GROUPS =

Each value of the mapping says how the attribute is stored:

- ``externalId``, ``userName``, ``name.givenName``, ``active`` and the ``employeeNumber`` of the
  enterprise extension are plain fields. ``externalId`` holds the identifier the identity
  provider gives to the user.
- ``displayName`` is a :class:`~scim2_django.storage.Computed` expression: the storage reads it,
  filters and sorts on it, and never writes it.
- ``emails`` is a :class:`~scim2_django.storage.Many`: the email addresses belong to the
  account, and writing the user replaces them.
- ``groups`` is a :class:`~scim2_django.storage.Link`: the teams are other resources, of the
  ``Group`` resource type.
- ``meta.created`` and ``meta.lastModified`` are the dates the storage fills.

The mapping checks every path against the :class:`~scim2_models.User` model when the module is
imported. A misspelled path raises a :class:`ValueError` at once.

Map the teams to groups
-----------------------

The teams become :class:`~scim2_models.Group` resources. Their members link back to the users:

.. literalinclude:: ../examples/scim.py
   :language: python
   :start-at: GROUPS =
   :end-before: def create_provider

Describe the service
--------------------

A :class:`~scim2_models.ScimProvider` describes the service: the schemas of its resources, its
resource types, and the features it supports. Build it from the mappings: the schemas of each
mapping, and a resource type built from the model of each mapping.
:meth:`~scim2_django.storage.ResourceMapping.schemas` restricts each schema to the attributes
the mapping stores, so that clients only send what the server keeps:

.. literalinclude:: ../examples/scim.py
   :language: python
   :pyobject: create_provider

The service supports PATCH, bulk requests, filtering, sorting and ETags, which the storage
implements. :doc:`scim2_models:how-to/describe-a-scim-service` describes the provider in full.

Create the site
---------------

A :class:`~scim2_django.storage.DjangoStorage` reads and writes the resources in the models, with
the mapping of each resource type. A :class:`~scim2_django.ScimSite` serves the SCIM endpoints of
the provider over the storage:

.. literalinclude:: ../examples/scim.py
   :language: python
   :start-at: def accept_anyone

The site refuses every request until it receives an ``authenticate`` function.
``accept_anyone`` accepts every client, which only suits local development.

Serve the endpoints
-------------------

Add ``scim2_django`` and the application of the models to :setting:`INSTALLED_APPS`:

.. code-block:: python

   INSTALLED_APPS = [
       ...,
       "scim2_django",
       "examples",
   ]

Include the URLs of the site in the URLconf of the project, as the URLs of the Django admin:

.. literalinclude:: ../examples/urls.py
   :language: python
   :lines: 1-2,5,7-8,11

The endpoints live under ``/scim/v2``. To serve them under another path, or to serve several SCIM
servers from one project, read :doc:`how-to/choose-the-urls`.

Check the mappings
------------------

With ``scim2_django`` in :setting:`INSTALLED_APPS`, the :djadmin:`check` command checks the
fields of every mapping. It reports a field that does not exist, or that cannot hold its
attribute, such as a text attribute mapped to an integer field:

.. code-block:: console

   $ python manage.py check
   System check identified no issues (0 silenced).

The storage refuses to serve a mapping with errors. The :ref:`system-checks` list every error and
warning.

Serve a request
---------------

Create the tables of the models:

.. code-block:: console

   $ python manage.py makemigrations examples
   $ python manage.py migrate

The project now answers SCIM requests. The examples send them with the
:ref:`test client <django:test-client>` of Django, which simulates HTTP requests without running
a server. Create a user:

.. doctest::

   >>> from django.test import Client
   >>> client = Client()
   >>> USER = "urn:ietf:params:scim:schemas:core:2.0:User"
   >>> response = client.post(
   ...     "/scim/v2/Users",
   ...     {
   ...         "schemas": [USER],
   ...         "userName": "bjensen",
   ...         "name": {"givenName": "Barbara", "familyName": "Jensen"},
   ...         "emails": [{"value": "bjensen@example.com", "type": "work", "primary": True}],
   ...     },
   ...     content_type="application/scim+json",
   ... )
   >>> response.status_code
   201
   >>> user = response.json()
   >>> user["displayName"]
   'Barbara Jensen'
   >>> user["active"]
   True
   >>> user["meta"]["location"]
   'http://testserver/scim/v2/Users/...'

The response comes from the new ``Account`` record. ``displayName`` comes from the computed
expression, and ``active`` from the default of the ``active`` field:

.. doctest::

   >>> from examples.models import Account
   >>> account = Account.objects.get(login="bjensen")
   >>> account.first_name, account.emails.get().address
   ('Barbara', 'bjensen@example.com')

Create a group with this user as a member. The member links to the user, with its URL:

.. doctest::

   >>> GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
   >>> response = client.post(
   ...     "/scim/v2/Groups",
   ...     {"schemas": [GROUP], "displayName": "Admins", "members": [{"value": user["id"]}]},
   ...     content_type="application/scim+json",
   ... )
   >>> response.json()["members"]
   [{'value': '...', '$ref': 'http://testserver/scim/v2/Users/...', 'display': 'Barbara Jensen'}]

Search the users. The storage turns the filter and the sort into a database query, so the
database only returns the matching records:

.. doctest::

   >>> response = client.get(
   ...     "/scim/v2/Users",
   ...     {"filter": 'emails[type eq "work"] and groups pr', "sortBy": "userName"},
   ... )
   >>> response.json()["totalResults"], response.json()["Resources"][0]["userName"]
   (1, 'bjensen')

An attribute the server does not store is refused, rather than lost. Errors come back as SCIM
error responses, not as the HTML error pages of Django:

.. doctest::

   >>> response = client.post(
   ...     "/scim/v2/Users",
   ...     {"schemas": [USER], "userName": "jsmith", "phoneNumbers": [{"value": "555-1234"}]},
   ...     content_type="application/scim+json",
   ... )
   >>> response.status_code, response.json()["schemas"]
   (400, ['urn:ietf:params:scim:api:messages:2.0:Error'])

Run the server
--------------

Run the :djadmin:`development server <runserver>` of the project:

.. code-block:: console

   $ python manage.py runserver

Any SCIM client can then reach it at ``http://localhost:8000/scim/v2``. In another terminal,
create a user:

.. tab-set::
   :class: outline

   .. tab-item:: curl
      :sync: curl

      .. code-block:: console

          $ curl -s -X POST http://localhost:8000/scim/v2/Users \
              -H "Content-Type: application/scim+json" \
              -d '{"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "bjensen"}' \
              | python -m json.tool
          {
              "schemas": [
                  "urn:ietf:params:scim:schemas:core:2.0:User"
              ],
              "id": "da42bb3d-7ea5-490f-bdd3-6d7cb47a861f",
              "meta": {
                  "resourceType": "User",
                  "created": "2026-10-12T08:08:58.512415Z",
                  "lastModified": "2026-10-12T08:08:58.512415Z",
                  "location": "http://localhost:8000/scim/v2/Users/da42bb3d-7ea5-490f-bdd3-6d7cb47a861f",
                  "version": "W/\"1\""
              },
              "userName": "bjensen",
              "active": true
          }

   .. tab-item:: scim2-cli
      :sync: scim2-cli

      .. code-block:: console

          $ pip install scim2-cli
          $ scim2 --url http://localhost:8000/scim/v2 create user --user-name bjensen
          {
              "schemas": [
                  "urn:ietf:params:scim:schemas:core:2.0:User"
              ],
              "id": "da42bb3d-7ea5-490f-bdd3-6d7cb47a861f",
              "meta": {
                  "resourceType": "User",
                  "created": "2026-10-12T08:08:58.512415Z",
                  "lastModified": "2026-10-12T08:08:58.512415Z",
                  "location": "http://localhost:8000/scim/v2/Users/da42bb3d-7ea5-490f-bdd3-6d7cb47a861f",
                  "version": "W/\"1\""
              },
              "userName": "bjensen",
              "active": true
          }

Check the server
----------------

:doc:`scim2-tester <scim2_tester:index>` checks that a SCIM server complies with the RFCs. Its
:func:`~scim2_tester.check_server` function sends the requests an identity provider would send,
and checks each response. The WSGI engine of :doc:`scim2-client <scim2_client:index>` calls the
WSGI application of the project directly, without a network:

.. code-block:: console

   $ pip install scim2-tester

Run the checks. The last line lists the title of each failed check. The list is empty when the
server passes every check:

.. doctest::

   >>> from django.core.wsgi import get_wsgi_application
   >>> from scim2_client.engines.wsgi import WSGISCIMClient
   >>> from scim2_tester import Status
   >>> from scim2_tester import check_server
   >>> scim_client = WSGISCIMClient(get_wsgi_application(), base_url="http://localhost/scim/v2")
   >>> scim_client.discover()
   >>> results = check_server(scim_client)
   >>> [result.title for result in results if result.status in (Status.ERROR, Status.CRITICAL)]
   []

In the tests of the project, :class:`~scim2_tester.testing.ScimServerContract` runs the same
checks as a pytest test, with a subtest per result. It needs the ``testing`` extra:

.. code-block:: console

   $ pip install "scim2-tester[testing]"

Subclass it, and give it a ``scim_client`` fixture. Django closes the database connection at the
end of each request. As the Django test client does, keep it open across the requests:

.. literalinclude:: ../tests/test_scim_tester.py
   :language: python

The ``django_db`` mark of `pytest-django <https://pytest-django.readthedocs.io>`_ gives the test
access to the database.

Next steps
----------

The server now serves the accounts and the teams of the intranet:

- To authenticate the clients, and tell the service who the client is, read
  :doc:`how-to/authenticate-the-clients`.
- To tie the changes of each request to a transaction, read :doc:`how-to/commit-the-changes`.
- To map other attributes, such as write-only or read-only ones, and to read the errors of the
  checks, read the :ref:`storage reference <storage-reference>`.
