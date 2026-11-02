How-to guides
=============

Use these guides to fit a SCIM server into a Django project. They assume a
:class:`~scim2_django.ScimSite` included in the URLconf, such as the one of the
:doc:`overview <../overview>`.

- :doc:`authenticate-the-clients`, to refuse the requests of unknown clients and tell the
  service who the client is;
- :doc:`choose-the-urls`, to serve the endpoints under a path, serve several SCIM servers from one
  project, and build the URLs of the endpoints;
- :doc:`serve-behind-a-proxy`, to make the URLs of the resources use the host and scheme the proxy
  received;
- :doc:`limit-the-size-of-the-requests`, to protect the memory of the server;
- :doc:`commit-the-changes`, to tie each SCIM operation to a database transaction.

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
