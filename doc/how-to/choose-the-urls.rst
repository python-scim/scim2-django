Choose the URLs
===============

This guide serves the SCIM endpoints under a chosen path, serves several SCIM servers from one
project, and builds the URLs of the endpoints. It addresses developers whose project includes a
:class:`~scim2_django.ScimSite`. The host and scheme of the URLs behind a reverse proxy are
covered by :doc:`serve-behind-a-proxy`.

Serve the endpoints under a path
--------------------------------

The path of the :func:`~django.urls.path` that includes :attr:`ScimSite.urls
<scim2_django.ScimSite.urls>` is the root of the SCIM endpoints. End it with a slash:

.. code-block:: python

   urlpatterns = [
       path("api/scim/", site.urls),
   ]

The users then live at ``/api/scim/Users``. To serve the endpoints at the root of the project,
pass an empty path: ``path("", site.urls)``.

The SCIM endpoints ignore a trailing slash: ``/api/scim/Users/`` serves the same search as
``/api/scim/Users``.

Serve several SCIM servers
--------------------------

To serve several SCIM servers from one project, such as one per tenant, create one
:class:`~scim2_django.ScimSite` per server. Give each one its own storage and its own ``name``,
and include each one under its own path:

.. code-block:: python

   tenant_a = ScimSite(storage_a, provider, name="tenant_a", authenticate=authenticate)
   tenant_b = ScimSite(storage_b, provider, name="tenant_b", authenticate=authenticate)

   urlpatterns = [
       path("a/scim/v2/", tenant_a.urls),
       path("b/scim/v2/", tenant_b.urls),
   ]

The ``name`` is the instance namespace of the URLs of the site. Its default value is ``scim``.

Build the URL of an endpoint
----------------------------

Each SCIM route has a URL name, listed in the :ref:`reference <url-names>`. Pass the name,
prefixed with the ``name`` of the site, to :func:`~django.urls.reverse`. The endpoint of a
resource type, such as ``Users``, is the ``endpoint`` argument:

.. code-block:: python

   reverse("tenant_a:query", kwargs={"endpoint": "Users", "resource_id": "2819c223"})
   # '/a/scim/v2/Users/2819c223'
   reverse("tenant_a:service_provider_config")
   # '/a/scim/v2/ServiceProviderConfig'

The application namespace of every site is ``scim``. ``reverse("scim:query", ...)`` builds the
URL of the site given by ``current_app``. Without ``current_app``, it builds the URL of the site
whose ``name`` is ``scim``, or else of the last site included.

Keep the identifiers free of slashes
------------------------------------

Django decodes the path of a request before it finds the view. An identifier holding an encoded
slash, such as ``a%2Fb``, reaches the server as two path segments, and the request gets a
``404``. Have the storage generate identifiers without slashes.
