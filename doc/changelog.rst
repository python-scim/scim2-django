Changelog
=========

[0.1.0] - Unreleased
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
- Filters, sorts and pages run in the database, on SQLite and PostgreSQL. SQLite compares strings
  with the comparison key of the policy. PostgreSQL compares them in NFC, lowered unless case
  exact. The storage searches one resource type, or several at the root of the server. It pages
  by index, or with cursors that give stable pages (:rfc:`RFC 9865 <9865>`).
- ``manage.py check`` checks the fields of every mapping.
- An attribute can be stored in a model related by a reverse one-to-one relation, such as a table
  beside the users, with a path such as ``"scim__external_id"``. The storage creates the related
  record at its first value.
- A :class:`~scim2_django.storage.Transformed` field holds an attribute in another form, such as
  an ``id`` made of a prefix and the primary key, or the hash of a password. Filters and sorts
  compare the value clients read, and a search by ``id`` uses the index of the field.
- An :class:`~scim2_django.storage.Entry` stores a multi-valued attribute holding one entry at
  most in fields of the resource model, such as the ``emails`` of a user in its ``email`` field.
- :func:`~scim2_django.auth.user_mapping` and :func:`~scim2_django.auth.group_mapping` serve the
  users of :setting:`AUTH_USER_MODEL` and the groups of :mod:`django.contrib.auth`, without any
  table. The ``id`` is the primary key after a prefix, the password is hashed by Django, and the
  version is a hash of the resource.
- ``meta.created`` and ``meta.lastModified`` are optional in a mapping. Without them, the
  resources have no such date.
- The version field is optional in a mapping. Without it, ``meta.version`` is a hash of the whole
  resource, which changes with any change of the resource, its links included.
