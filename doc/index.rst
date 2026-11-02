scim2-django
============

scim2-django serves a SCIM 2.0 server from a :doc:`Django <django:index>` project, as
:rfc:`RFC 7643 <7643>` and :rfc:`RFC 7644 <7644>` define it. It exposes the resource, search,
bulk and discovery endpoints. It is built upon :doc:`scim2-server <scim2_server:index>` and
:doc:`scim2-models <scim2_models:index>`.

scim2-django serves the SCIM protocol. The project that includes it provides the rest:

- a :class:`~scim2_server.storage.ScimStorage`, which reads and writes the resources;
- a :class:`~scim2_models.ScimProvider`, which declares the resource types the server serves and
  the features it supports;
- a function authenticating the clients. Without it, the server refuses every request.

This documentation addresses Python developers who build a SCIM server with Django. It assumes
familiarity with Django projects. It covers what is specific to Django: the storage, the service
and the protocol rules are described in the :doc:`scim2-server documentation <scim2_server:index>`.

.. code-block:: shell

   pip install scim2-django

Choose a path
-------------

:doc:`Overview <overview>` introduces the parts of a SCIM server built with Django, in the order
a project meets them.

:doc:`How-to guides <how-to/index>` show how to authenticate the clients, choose the URLs, serve
behind a reverse proxy, limit the size of the requests and commit the changes.

:doc:`Reference <reference>` lists the complete public API and the URL names.

.. toctree::
    :maxdepth: 2
    :hidden:

    Overview <overview>
    How-to guides <how-to/index>
    Reference <reference>
    Contributing <contributing>
    Changelog <changelog>
