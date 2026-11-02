Overview
========

scim2-django serves the **System for Cross-domain Identity Management** (**SCIM**) protocol from a
Django project. It exposes the endpoints of :rfc:`RFC 7644 <7644>` in the URLconf of the project,
and leaves the protocol to :doc:`scim2-server <scim2_server:index>` and the payloads to
:doc:`scim2-models <scim2_models:index>`.

scim2-django does not store the resources itself. The project provides a
:class:`~scim2_server.storage.ScimStorage` that reads and writes them, for example in its
database. The project also authenticates the clients, with a function it passes to
:class:`~scim2_django.ScimSite`.

Install scim2-django. It brings Django, scim2-server and scim2-models along:

.. code-block:: console

   $ pip install scim2-django

This page introduces the parts of a SCIM server built with Django, in the order a project meets
them. Follow it in order for a first tour. The :doc:`how-to guides <how-to/index>` cover the
authentication, the URLs, the reverse proxies, the size of the requests and the transactions,
and the :doc:`reference` lists the complete API.

Describe the service
--------------------

A :class:`~scim2_models.ScimProvider` describes the service: the resources it serves, and the
features it supports. The following service serves users, with the enterprise extension, and
groups. It supports PATCH, bulk requests, filtering, sorting and ETags:

.. literalinclude:: ../examples/scim.py
   :language: python
   :pyobject: create_provider

Announce filtering, sorting and ETags only when the storage implements them.
:doc:`scim2_models:how-to/describe-a-scim-service` describes the provider in full.

Create the site
---------------

A :class:`~scim2_django.ScimSite` serves the SCIM endpoints of a provider over a storage.
:class:`~scim2_server.memory.InMemoryStorage` keeps the resources in memory, until the process
stops:

.. literalinclude:: ../examples/scim.py
   :language: python
   :start-at: def accept_anyone

The site refuses every request until it receives an ``authenticate`` function.
``accept_anyone`` accepts every client, which only suits local development.
:doc:`how-to/authenticate-the-clients` replaces it with a real authentication.

To serve the resources of a database, write a storage as
:doc:`scim2_server:how-to/write-a-storage` describes, and pass it in place of the in-memory one.

Serve the endpoints
-------------------

Include the URLs of the site in the URLconf of the project, as the URLs of the Django admin:

.. literalinclude:: ../examples/urls.py
   :language: python
   :lines: 1-2,4,6-7,9

The endpoints live under ``/scim/v2``. :doc:`how-to/choose-the-urls` serves them under another
path, or serves several SCIM servers from one project.

Serve a request
---------------

The project now answers SCIM requests. The examples send them with the
:ref:`test client <django:test-client>` of Django, which simulates HTTP requests without running
a server. Create a user, and read it back at its location:

.. doctest::

   >>> from django.test import Client
   >>> client = Client()
   >>> response = client.post(
   ...     "/scim/v2/Users",
   ...     {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "bjensen"},
   ...     content_type="application/scim+json",
   ... )
   >>> response.status_code
   201
   >>> user = response.json()
   >>> user["meta"]["location"]
   'http://testserver/scim/v2/Users/...'
   >>> client.get(user["meta"]["location"]).json()["userName"]
   'bjensen'

A request the server refuses gets a SCIM error:

.. doctest::

   >>> response = client.get("/scim/v2/Users/unknown")
   >>> response.status_code, response.json()["schemas"]
   (404, ['urn:ietf:params:scim:api:messages:2.0:Error'])

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
              "id": "da42bb3d7ea5490fbdd36d7cb47a861f",
              "meta": {
                  "resourceType": "User",
                  "created": "2026-10-12T08:08:58.512415Z",
                  "lastModified": "2026-10-12T08:08:58.512415Z",
                  "location": "http://localhost:8000/scim/v2/Users/da42bb3d7ea5490fbdd36d7cb47a861f",
                  "version": "W/\"1\""
              },
              "userName": "bjensen"
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
              "id": "da42bb3d7ea5490fbdd36d7cb47a861f",
              "meta": {
                  "resourceType": "User",
                  "created": "2026-10-12T08:08:58.512415Z",
                  "lastModified": "2026-10-12T08:08:58.512415Z",
                  "location": "http://localhost:8000/scim/v2/Users/da42bb3d7ea5490fbdd36d7cb47a861f",
                  "version": "W/\"1\""
              },
              "userName": "bjensen"
          }

Check a server
--------------

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
access to the database, for a storage upon the Django ORM.
