Changelog
=========

[0.2.0] - 2026-11-26
--------------------

Added
^^^^^
- :meth:`ResourceMapping.schemas() <scim2_django.storage.ResourceMapping.schemas>` publishes
  ``required`` for an attribute whose field needs a value.
- ``manage.py check`` warns about a field that needs a value but that the storage never writes
  (``W002``), and refuses a write-only or never returned attribute mapped to a generated field
  (``E011``).

Changed
^^^^^^^
- Needs scim2-server 0.8.9 and scim2-models 0.12.7 or later.
- The storage reads the write-only and never returned attributes, such as the password hash of
  :func:`~scim2_django.auth.user_mapping`. A PUT without such a value keeps it, and a PATCH can
  remove it. Clients cannot filter nor sort on it.
- :class:`~scim2_django.storage.Attribute` no longer takes ``readable``. A
  :class:`~scim2_django.storage.Transformed` without ``expression`` only maps a write-only or
  never returned attribute.

Fixed
^^^^^
- A change that a foreign key refuses answers a ``409`` instead of a ``500``, unless
  :setting:`ATOMIC_REQUESTS <DATABASE-ATOMIC_REQUESTS>` defers the check to the commit, on
  PostgreSQL and SQLite.
- ``E003`` refuses a foreign key without null for a required :class:`~scim2_django.storage.Link`
  of an optional extension, which raised a database error.

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
