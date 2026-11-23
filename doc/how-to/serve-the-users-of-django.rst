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

Add a model beside the users and the groups
-------------------------------------------

The users and the groups have no field for ``externalId`` nor ``meta.lastModified``.
``externalId`` is an attribute of every resource (:rfc:`RFC 7643 §3.1 <7643#section-3.1>`), so
``/Schemas`` cannot tell clients the server does not store it. Without a field holding it, the
server refuses a write carrying it with a ``400`` error. Okta sends it in every user it creates.

Store these attributes in a model beside the users, and one beside the groups. Inherit from
:class:`~scim2_django.auth.models.AbstractUserScim` and
:class:`~scim2_django.auth.models.AbstractGroupScim`:

.. literalinclude:: ../../examples/models.py
   :language: python
   :pyobject: AccountScim

.. literalinclude:: ../../examples/models.py
   :language: python
   :pyobject: TeamScim

Create their tables with ``manage.py makemigrations`` and ``manage.py migrate``. The tables start
empty, and nothing needs to fill them: a user without record beside it has no ``externalId``
and no ``meta.lastModified``.

:func:`~scim2_django.auth.user_mapping` and :func:`~scim2_django.auth.group_mapping` find these
models through their one-to-one relation, and map the fields they keep: ``externalId``,
``meta.lastModified``, and ``meta.created`` for the groups. Only the groups created through SCIM
have a creation date. To leave out a field, set it to None in the model:

.. code-block:: python

   class AccountScim(AbstractUserScim):
       last_modified = None

Add ``scim2_django.auth`` to :setting:`INSTALLED_APPS`, so that a change outside SCIM changes
``meta.lastModified`` too: a save of a user or a group, or a change of the groups of a user. A
login changes nothing, unless Django upgrades the hash of the password. The records ``loaddata``
loads keep the dates of the fixture, but the links between users and groups it sets date them.
:meth:`~django.db.models.query.QuerySet.update`,
:meth:`~django.db.models.query.QuerySet.bulk_update` and raw SQL send no signal, so
``meta.lastModified`` misses their changes. ``meta.version`` does not: it is a hash of the
resource.

The models beside the users can hold other attributes, such as those of the enterprise
extension. Add a field with null or a default, as the storage creates the record of a user at its
first write, and map it through the relation:

.. code-block:: python

   users = user_mapping(
       {f"{ENTERPRISE}:employeeNumber": "scim__employee_number"},
       model=User[EnterpriseUser],
   )

A project with a user model of its own can rather add ``external_id`` to it, and map it as in
``user_mapping({"externalId": "external_id"})``. The groups still need a model beside them.

Connect Microsoft Entra ID
--------------------------

Entra ID finds the users it provisions by ``userName``, and the groups by ``displayName``. For an
application outside the gallery, it does not read ``/Schemas``. In the attribute mappings of the
provisioning, keep only the attributes the server stores, those of `Map the users and the
groups`_, and remove the others. Remove ``externalId`` too, unless the server stores it.

Connect Okta
------------

Okta finds the users it provisions by ``userName``, and sends ``externalId`` in every user it
creates: store it, as in `Add a model beside the users and the groups`_.

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
