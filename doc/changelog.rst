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
  exact. The storage searches one resource type at a time. It pages by index, or with cursors
  that give stable pages (:rfc:`RFC 9865 <9865>`).
- ``manage.py check`` checks the fields of every mapping.
