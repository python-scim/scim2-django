How-to guides
=============

These guides fit a SCIM server into a Django project. They assume a
:class:`~scim2_django.ScimSite` included in the URLconf, such as the one of the
:doc:`overview <../overview>`.

:doc:`authenticate-the-clients` refuses the requests of unknown clients, and tells the service
who the client is.

:doc:`choose-the-urls` serves the endpoints under a path, serves several SCIM servers from one
project, and builds the URLs of the endpoints.

:doc:`serve-behind-a-proxy` makes the URLs of the resources use the host and scheme the proxy
received.

:doc:`limit-the-size-of-the-requests` protects the memory of the server.

:doc:`commit-the-changes` ties each SCIM operation to a database transaction.

The scim2-server guides cover what does not depend on Django: writing a storage, authorizing
each operation, serving ``/Me`` and paging with cursors.

.. toctree::
    :maxdepth: 1
    :hidden:

    authenticate-the-clients
    choose-the-urls
    serve-behind-a-proxy
    limit-the-size-of-the-requests
    commit-the-changes
