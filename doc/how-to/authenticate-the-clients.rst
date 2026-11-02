Authenticate the clients
========================

Use this guide to refuse the SCIM requests of unknown clients, to pass the identity of the known
ones to the service, and to limit what each client may do. It addresses developers whose project
includes a :class:`~scim2_django.ScimSite`.

Without an ``authenticate`` function, a :class:`~scim2_django.ScimSite` answers every request
with a ``401``, except the requests on ``/ServiceProviderConfig``. Per
:rfc:`RFC 7643 §5 <7643#section-5>`, clients read the authentication schemes there before they
authenticate.

Write the authentication function
---------------------------------

The function receives the :class:`~django.http.HttpRequest`. It returns the *subject* of the
request, which is any object standing for the client: a user, the claims of a token, or a name.
To refuse the request, it raises :class:`~scim2_models.UnauthorizedException`.

The following function accepts the bearer tokens of a dictionary:

.. literalinclude:: ../../examples/authentication.py
   :language: python
   :pyobject: authenticate

Replace ``TOKENS`` with the validation of the tokens of your project, preferably with a library
dedicated to authentication, such as `Authlib <https://docs.authlib.org>`_.

The function may also raise :exc:`~django.core.exceptions.PermissionDenied`, which gives a
``403``, or :exc:`~django.http.Http404`, which gives a ``404``. The client receives these errors
as SCIM errors.

Pass the function to the site
-----------------------------

Pass the function as the ``authenticate`` parameter of :class:`~scim2_django.ScimSite`:

.. literalinclude:: ../../examples/authentication.py
   :language: python
   :start-at: site = ScimSite(

Announce the scheme
-------------------

Announce the authentication scheme in the ``authentication_schemes`` of the
:class:`~scim2_models.ServiceProviderConfig`:

.. literalinclude:: ../../examples/authentication.py
   :language: python
   :pyobject: create_provider

A refused client then receives a ``401`` with a :mdn:`WWW-Authenticate` header built from the
announced schemes, here ``Bearer realm="SCIM"``.

Use the subject
---------------

The service receives the subject of each request in
:attr:`ScimRequest.subject <scim2_server.requests.ScimRequest.subject>`. By default, every
authenticated client may run every operation, and ``/Me`` answers ``501``. To change this,
subclass :class:`~scim2_server.service.ScimService`:

- override :meth:`~scim2_server.service.ScimService.authorize` to limit what each client may do;
- override :meth:`~scim2_server.service.ScimService.me_target` to serve ``/Me``, with the
  resource of the authenticated client.

The following service lets the clients read, and refuses the other operations with a ``403``:

.. literalinclude:: ../../examples/authorization.py
   :language: python
   :lines: 2,4-5,10-17

Pass an instance as the ``service`` parameter of :class:`~scim2_django.ScimSite`. Give it the
:setting:`SECRET_KEY` of your project as its ``secret``, as the default service does. When the
configuration announces cursor pagination, the service needs this secret to encrypt the cursors:

.. literalinclude:: ../../examples/authorization.py
   :language: python
   :lines: 1,3,9,19-

To decide according to the subject, such as with the scopes of its token, read
:doc:`scim2_server:how-to/authenticate-the-clients`. To serve ``/Me``, read
:doc:`scim2_server:how-to/serve-the-me-endpoint`.

Avoid the session cookie
------------------------

The SCIM views are exempt from :class:`~django.middleware.csrf.CsrfViewMiddleware` and from
:class:`~django.contrib.auth.middleware.LoginRequiredMiddleware`. Authenticate the clients with
a header, such as ``Authorization``. Never accept the session cookie of your project: a browser
sends it with the requests other websites trigger, and the views do not check CSRF tokens.

Errors raised before the SCIM views, such as the ``DisallowedHost``
of a request on an unknown host, follow the error handlers of your project, and are not SCIM
errors.
