Serve the users of django.contrib.auth
======================================

Use this guide to let identity providers, such as Microsoft Entra ID and Okta, provision the users
and the groups of :mod:`django.contrib.auth`. It addresses developers whose project keeps its
users in :setting:`AUTH_USER_MODEL` and its groups in ``auth.Group``. It assumes a
:class:`~scim2_django.ScimSite` included in the URLconf, such as the one of the
:doc:`overview <../overview>`. To store the resources in other models, read the overview.

Map the users and the groups
----------------------------

:func:`~scim2_django.auth.user_mapping` and :func:`~scim2_django.auth.group_mapping` return the
mappings of the users and of the groups. They need no table: each attribute maps to a field the
models already have. Build the provider of the server from their schemas, and serve them with a
:class:`~scim2_django.storage.DjangoStorage`:

.. literalinclude:: ../../examples/accounts.py
   :language: python
   :pyobject: create_provider

.. literalinclude:: ../../examples/accounts.py
   :language: python
   :start-at: users = user_mapping

.. warning::

   ``accept_anyone`` lets any client in, and this server can change the password of any user,
   a superuser included. Authenticate the clients, as in :doc:`authenticate-the-clients`.

The mapping of the users holds the attributes the user model has fields for: ``userName``,
``name.givenName``, ``name.familyName``, one ``work`` email address, ``active``, ``password``,
``groups`` and ``meta.created``. A password follows the validators of
:setting:`AUTH_PASSWORD_VALIDATORS`. A user created without password, or with an empty one, gets
an unusable one, as with :meth:`~django.contrib.auth.models.UserManager.create_user`. The mapping of the groups holds
``displayName`` and ``members``.

The resources have no ``meta.lastModified``. Their ``meta.version`` is a hash of the resource:
adding a member to a group changes the version of the group and of the user, and a login does
not change it.

Pass a dictionary to add, change or remove attributes, as
:class:`~scim2_django.storage.ResourceMapping` takes them. None removes an attribute:

.. code-block:: python

   users = user_mapping({"password": None})

Choose the identifiers
----------------------

A user and a group may have the same primary key, so the ``id`` is the primary key after a
prefix: ``u12`` for a user, ``g3`` for a group. Per :rfc:`RFC 7643 §3.1 <7643#section-3.1>`, two
resources never share an ``id``, even of different types.

A user model whose primary key is a UUID can do without the prefix:

.. code-block:: python

   users = user_mapping(prefix="")

Choose the prefix before connecting the first identity provider. The identity providers keep the
``id`` of each resource, and changing the prefix changes every ``id``.

Store externalId
----------------

``externalId`` is an attribute of every resource (:rfc:`RFC 7643 §3.1 <7643#section-3.1>`), so
``/Schemas`` cannot tell clients the server does not store it. Without a field holding it, the
server refuses a write carrying it with a ``400`` error. Okta sends it in every user it creates.

Store it in a model beside the users, and one beside the groups, with a one-to-one relation
whose ``related_name`` is the start of the path:

.. literalinclude:: ../../examples/models.py
   :language: python
   :pyobject: AccountScim

.. literalinclude:: ../../examples/models.py
   :language: python
   :pyobject: TeamScim

Map ``externalId`` through the relation, as in ``user_mapping({"externalId":
"scim__external_id"})``. These tables start empty: the storage creates the record of a user at the
first ``externalId`` a client gives it.

Connect Microsoft Entra ID
--------------------------

Entra ID finds the users it provisions by ``userName``, and the groups by ``displayName``. For an
application outside the gallery, it does not read ``/Schemas``. In the attribute mappings of the
provisioning, keep only the attributes the server stores, those of `Map the users and the
groups`_, and remove the others. Remove ``externalId`` too, unless the server stores it.

Connect Okta
------------

Okta finds the users it provisions by ``userName``, and sends ``externalId`` in every user it
creates: store it, as in `Store externalId`_.

Okta sends a password even when it does not synchronize the passwords. For a project whose users
log in through the identity provider only, give every user an unusable password, whatever the
client sends:

.. code-block:: python

   from django.contrib.auth.hashers import make_password

   from scim2_django.storage import Transformed

   unusable = Transformed("password", to_field=lambda value: make_password(None))
   users = user_mapping({"password": unusable})

Protect the accounts of the project
-----------------------------------

The identity provider takes over a user whose ``userName`` matches one of its own users, a
superuser included: it may deactivate the user, or delete it. Deleting a user deletes the records
of the project that refer to it with ``on_delete=CASCADE``. Before connecting the identity
provider, rename the local accounts it must not manage.
