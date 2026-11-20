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
- ``meta.created`` and ``meta.lastModified`` are optional in a mapping. Without them, the
  resources have no such date.
- The version field is optional in a mapping. Without it, ``meta.version`` is a hash of the whole
  resource, which changes with any change of the resource, its links included.
