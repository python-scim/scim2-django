Changelog
=========

[Unreleased]
------------

Added
^^^^^
- :meth:`ResourceMapping.schemas() <scim2_django.storage.ResourceMapping.schemas>` publishes
  ``required`` for an attribute the storage cannot create a resource without, and for a complex
  attribute holding such a sub-attribute.
- The warning ``scim2_django.W002`` reports a field of the resource model that needs a value, and
  that the storage never writes. Creating a resource then raises a database error.
- The check ``scim2_django.E011`` refuses an attribute that the storage never reads, mapped to a
  generated field.

Changed
^^^^^^^
- An attribute mapped with ``readable=False``, or to a
  :class:`~scim2_django.storage.Transformed` without ``expression``, is published ``writeOnly``
  and never returned.
- An attribute that the server never returns, such as a write-only one, is published optional,
  even when its schema requires it. It cannot be filtered nor sorted on. A replacement without
  its value keeps it, so a PATCH cannot remove it. The check ``scim2_django.E007`` refuses it in
  the entries of a :class:`~scim2_django.storage.Many` or an
  :class:`~scim2_django.storage.Entry`. A link does not display it.
- A mapping refuses an attribute that clients could neither read nor write, and an immutable
  attribute that the storage would not read.
- The check ``scim2_django.E003`` refuses a foreign key without null for a required
  :class:`~scim2_django.storage.Link` of an optional extension. A resource without the extension
  raised a database error.

[0.1.1] - 2026-11-24
--------------------

Changed
^^^^^^^
- Needs scim2-server 0.8.8 or later.

Fixed
^^^^^
- A cursor whose sort value the field reads as an integer beyond a BIGINT raises
  ``invalidCursor``, instead of an error of the database.

[0.1.0] - 2026-11-24
--------------------

Added
^^^^^
- :class:`~scim2_django.ScimSite`, a SCIM server to include in the URLconf of a Django project.
  It serves the endpoints of :rfc:`RFC 7644 <7644>` over a
  :class:`~scim2_server.storage.ScimStorage`. The SCIM protocol is handled by
  :doc:`scim2-server <scim2_server:index>`.
- The server refuses every request until your project passes a function authenticating the
  clients. ``/ServiceProviderConfig`` stays open.
- One site per tenant, each with its own URL namespace.
- :class:`~scim2_django.storage.DjangoStorage` stores the resources in the models of the project.
  A :class:`~scim2_django.storage.ResourceMapping` maps each SCIM attribute to a field, a
  computed expression, the entries of another model, or links to other resources.
  ``manage.py check`` checks the fields of every mapping.
- Filters, sorts and pages run in the database, on SQLite, PostgreSQL and MariaDB. SQLite compares
  strings with the comparison key of the policy. PostgreSQL and MariaDB compare them lowered unless
  case exact, by code point, and in NFC on PostgreSQL. The storage searches one resource type, or
  several at the root of the server. It pages by index, or with cursors that give stable pages
  (:rfc:`RFC 9865 <9865>`).
- ``meta.created``, ``meta.lastModified`` and the version field are optional in a mapping.
  Without the dates, the resources have none. Without a version field, ``meta.version`` is a hash
  of the whole resource, which changes with any change of the resource, its links included.
- An attribute can be stored in a model related by a reverse one-to-one relation, such as a table
  beside the users, with a path such as ``"scim__external_id"``. The storage creates the related
  record at its first value. ``meta.created`` and ``meta.lastModified`` can be stored there too:
  each write then creates the related record.
- A :class:`~scim2_django.storage.Transformed` field holds an attribute in another form, such as
  an ``id`` made of a prefix and the primary key, or the hash of a password. Filters and sorts
  compare the value clients read, and a search by ``id`` uses the index of the field.
- An :class:`~scim2_django.storage.Entry` stores a multi-valued attribute holding one entry at
  most in fields of the resource model, such as the ``emails`` of a user in its ``email`` field.
- :func:`~scim2_django.auth.user_mapping` and :func:`~scim2_django.auth.group_mapping` serve the
  users of :setting:`AUTH_USER_MODEL` and the groups of :mod:`django.contrib.auth`, without any
  table. The ``id`` is the primary key after a prefix, the password is hashed by Django, and the
  version is a hash of the resource.
- :class:`~scim2_django.auth.models.AbstractUserScim` and
  :class:`~scim2_django.auth.models.AbstractGroupScim` add a model beside the users and the
  groups, for ``externalId``, ``meta.lastModified`` and the attributes of the project. With
  ``scim2_django.auth`` in :setting:`INSTALLED_APPS`, a change outside SCIM changes
  ``meta.lastModified`` too.
