Authenticate the clients
========================

Use this guide to refuse the SCIM requests of unknown clients, and to pass the identity of the
known ones to the service. It addresses developers whose project includes a
:class:`~scim2_django.ScimSite`. To decide what each client may do, read
:doc:`scim2_server:how-to/authenticate-the-clients`.

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

Replace ``TOKENS`` with the validation of the tokens of the project, preferably with a library
dedicated to authentication.

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
:attr:`ScimRequest.subject <scim2_server.requests.ScimRequest.subject>`. Two of its methods read
it: :meth:`~scim2_server.service.ScimService.authorize` accepts or refuses each operation, and
:meth:`~scim2_server.service.ScimService.me_target` finds the resource ``/Me`` stands for. To
override them, subclass :class:`~scim2_server.service.ScimService`, and pass an instance as the
``service`` parameter of :class:`~scim2_django.ScimSite`.

Avoid the session cookie
------------------------

The SCIM views are exempt from :class:`~django.middleware.csrf.CsrfViewMiddleware` and from
:class:`~django.contrib.auth.middleware.LoginRequiredMiddleware`. Authenticate the clients with
a header, such as ``Authorization``. Never accept the session cookie of the project: a browser
sends it with the requests other websites trigger, and the views do not check CSRF tokens.

Errors raised before the SCIM views, such as the ``DisallowedHost``
of a request on an unknown host, follow the error handlers of the project, and are not SCIM
errors.
