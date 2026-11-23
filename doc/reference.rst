Reference
=========

This reference describes the public scim2-django API, the URL names of the SCIM endpoints and the
system checks of the mappings. The :doc:`scim2-server reference <scim2_server:reference/index>`
describes the storage contract, the service and the other classes the site relies on.

Site
----

The SCIM server, to include in the URLconf of a project.

.. autoclass:: scim2_django.ScimSite
   :members: urls, get_urls, authenticate, read_request, read_body, serve

.. _storage-reference:

Storage
-------

The storage of the resources in the models of a project, and the mappings between SCIM models
and Django models.

.. autoclass:: scim2_django.storage.DjangoStorage
   :members: operation, get, search, create, update, delete

.. autoclass:: scim2_django.storage.ResourceMapping
   :members: schemas, check

.. autoclass:: scim2_django.storage.Attribute

.. autoclass:: scim2_django.storage.Computed

.. autoclass:: scim2_django.storage.Transformed

.. autoclass:: scim2_django.storage.Many

.. autoclass:: scim2_django.storage.Entry

.. autoclass:: scim2_django.storage.Link

Users of django.contrib.auth
----------------------------

The mappings of the users of :setting:`AUTH_USER_MODEL` and of the groups of
:mod:`django.contrib.auth`. Read :doc:`how-to/serve-the-users-of-django`.

.. autofunction:: scim2_django.auth.user_mapping

.. autofunction:: scim2_django.auth.group_mapping

.. _system-checks:

System checks
-------------

With ``scim2_django`` in :setting:`INSTALLED_APPS`, the :doc:`system check framework
<django:topics/checks>` checks the fields of every :class:`~scim2_django.storage.ResourceMapping`
once the URLconf is loaded. A storage refuses to serve a mapping with errors, and raises
:class:`~django.core.exceptions.ImproperlyConfigured`.

The checks report the following errors and warnings:

.. list-table::
   :header-rows: 1

   * - ID
     - Reported when
   * - ``scim2_django.E001``
     - An attribute, or the version, is mapped to a name that is not a field of the model.
   * - ``scim2_django.E002``
     - An attribute is mapped to a relation by its name. A relation is mapped with
       :class:`~scim2_django.storage.Many` or :class:`~scim2_django.storage.Link`.
   * - ``scim2_django.E003``
     - The relation of a :class:`~scim2_django.storage.Many` or a
       :class:`~scim2_django.storage.Link` does not fit it. A ``Many`` needs the reverse relation
       of a foreign key with ``on_delete=CASCADE`` and without null. A ``Link`` holding several
       entries needs a many-to-many relation. A ``Link`` holding one entry needs a foreign key or
       a one-to-one field, with ``null=True`` unless the attribute is required.
   * - ``scim2_django.E004``
     - The version is not an integer field.
   * - ``scim2_django.E005``
     - ``id`` is mapped to a field that is neither the primary key nor unique without null.
   * - ``scim2_django.E006``
     - ``meta.created`` or ``meta.lastModified`` is mapped to a field that is not a
       :class:`~django.db.models.DateTimeField`.
   * - ``scim2_django.E007``
     - The entries of a :class:`~scim2_django.storage.Many` hold a write-only value, or a
       read-only value next to written ones. Each write replaces the entries, so these values
       would be lost. Or a field of an :class:`~scim2_django.storage.Entry` is write-only, cannot
       hold no value, or is read-only next to written ones: the entry could not be removed.
   * - ``scim2_django.E008``
     - A string attribute is mapped to a field or an expression that does not hold text.
   * - ``scim2_django.E009``
     - The expression of a :class:`~scim2_django.storage.Computed` or a
       :class:`~scim2_django.storage.Transformed` cannot be computed on the model.
   * - ``scim2_django.E010``
     - An attribute is mapped to a field of another model, other than through one reverse
       one-to-one relation of the resource model. Or ``id``, a date or an entry is mapped
       through a relation. Or the related model has a field without default that the mapping
       leaves out, so the storage cannot create its records.
   * - ``scim2_django.W001``
     - A readable string attribute is mapped to a text field that converts the values it reads,
       such as a field decrypting them. Filters and sorts compare the stored values, not the
       values clients read.

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
