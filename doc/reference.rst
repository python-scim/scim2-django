Reference
=========

This reference describes the public scim2-django API and the URL names of the SCIM endpoints.
The :doc:`scim2-server reference <scim2_server:reference/index>` describes the storage contract,
the service and the other classes the site relies on.

Site
----

The SCIM server, to include in the URLconf of a project.

.. autoclass:: scim2_django.ScimSite
   :members: urls, get_urls, authenticate, read_request, read_body, serve

.. _url-names:

URL names
---------

:attr:`ScimSite.urls <scim2_django.ScimSite.urls>` names each route of
:data:`~scim2_server.routing.ROUTES`. The application namespace is ``scim``, and the instance
namespace is the ``name`` of the site. ``endpoint`` stands for the endpoint of a resource type,
such as ``Users``, and ``resource_id`` for an identifier.

Several routes share a path, such as ``search`` and ``create``. Their names give the same URL.

The URL names are the following:

.. list-table::
   :header-rows: 1

   * - Name
     - Method
     - Path
   * - ``service_provider_config``
     - GET
     - ``ServiceProviderConfig``
   * - ``resource_types``
     - GET
     - ``ResourceTypes``
   * - ``resource_type``
     - GET
     - ``ResourceTypes/<resource_id>``
   * - ``schemas``
     - GET
     - ``Schemas``
   * - ``schema``
     - GET
     - ``Schemas/<resource_id>``
   * - ``bulk``
     - POST
     - ``Bulk``
   * - ``me_query``
     - GET
     - ``Me``
   * - ``me_replace``
     - PUT
     - ``Me``
   * - ``me_patch``
     - PATCH
     - ``Me``
   * - ``me_delete``
     - DELETE
     - ``Me``
   * - ``me_create``
     - POST
     - ``Me``
   * - ``search_root``
     - GET
     - the root of the SCIM endpoints
   * - ``search_root_with_body``
     - POST
     - ``.search``
   * - ``search_with_body``
     - POST
     - ``<endpoint>/.search``
   * - ``search``
     - GET
     - ``<endpoint>``
   * - ``create``
     - POST
     - ``<endpoint>``
   * - ``query``
     - GET
     - ``<endpoint>/<resource_id>``
   * - ``replace``
     - PUT
     - ``<endpoint>/<resource_id>``
   * - ``patch``
     - PATCH
     - ``<endpoint>/<resource_id>``
   * - ``delete``
     - DELETE
     - ``<endpoint>/<resource_id>``
   * - ``fallback``
     - any
     - any other path, answered with a SCIM error
