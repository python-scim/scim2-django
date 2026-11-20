scim2-django
============

scim2-django serves a SCIM 2.0 server from a :doc:`Django <django:index>` project, as
:rfc:`RFC 7643 <7643>` and :rfc:`RFC 7644 <7644>` define it. It exposes the resource, search,
bulk and discovery endpoints. It is built upon :doc:`scim2-server <scim2_server:index>` and
:doc:`scim2-models <scim2_models:index>`.

scim2-django serves the SCIM protocol, and stores the resources in the models of your project.
Your project provides the rest:

- a :class:`~scim2_django.storage.ResourceMapping` for each resource type, which maps the SCIM
  attributes to the fields of a model;
- a :class:`~scim2_models.ScimProvider`, which declares the resource types the server serves and
  the features it supports;
- a function authenticating the clients. Without it, the server refuses every request.

This documentation addresses Python developers who build a SCIM server with Django. It assumes
familiarity with Django projects. It covers what is specific to Django. The
:doc:`scim2-server documentation <scim2_server:index>` describes the storage contract, the
service and the protocol rules.

.. code-block:: shell

   pip install scim2-django

Choose a path
-------------

:doc:`Overview <overview>` builds a SCIM server over the models of an existing project, step
by step.

:doc:`How-to guides <how-to/index>` show how to authenticate the clients, choose the URLs, serve
behind a reverse proxy, limit the size of the requests and commit the changes.

:doc:`Reference <reference>` lists the complete public API, the URL names and the system checks.

.. toctree::
    :maxdepth: 2
    :hidden:

    Overview <overview>
    How-to guides <how-to/index>
    Reference <reference>
    Contributing <contributing>
    Changelog <changelog>
