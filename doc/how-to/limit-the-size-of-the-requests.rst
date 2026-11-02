Limit the size of the requests
==============================

Use this guide to bound the size of the request bodies the server reads, and so protect its
memory. It addresses developers whose project includes a :class:`~scim2_django.ScimSite`. To set
the limits of the bulk operations themselves, such as ``maxOperations``, read
:doc:`scim2_models:how-to/describe-a-scim-service`.

The server reads the request bodies within two limits:

- :setting:`DATA_UPLOAD_MAX_MEMORY_SIZE`, the limit of the project, applies to every request.
  Its default value is 2.5 MB.
- ``maxPayloadSize``, in the ``bulk`` section of the :class:`~scim2_models.ServiceProviderConfig`,
  applies to the bulk requests. The server reads at most one byte more.

A request beyond one of the limits gets a ``413``, as a SCIM error.

Set the limits
--------------

1. Set ``maxPayloadSize`` to the largest bulk request the server accepts.
2. Set :setting:`DATA_UPLOAD_MAX_MEMORY_SIZE` to the largest request the project accepts. Keep
   it above ``maxPayloadSize``: a bulk request within ``maxPayloadSize`` but beyond
   :setting:`DATA_UPLOAD_MAX_MEMORY_SIZE` gets a ``413`` too.

Limit the bodies under ASGI
---------------------------

Under ASGI, Django reads the whole body of a request before it calls the view. It keeps the body
in memory up to :setting:`FILE_UPLOAD_MAX_MEMORY_SIZE`, and in a temporary file beyond. The
server still answers ``413`` beyond the limits, but only once the body is received.

To refuse large bodies before they reach Django, set a limit in the ASGI server or in the
reverse proxy, such as ``client_max_body_size`` with nginx.
