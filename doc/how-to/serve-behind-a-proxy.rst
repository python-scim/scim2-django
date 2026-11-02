Serve behind a reverse proxy
============================

Use this guide to make the URLs of the resources, such as ``meta.location`` and the
:mdn:`Location` header, use the host and scheme the reverse proxy received. It addresses
developers who deploy a project including a :class:`~scim2_django.ScimSite` behind a reverse
proxy. To deploy Django itself, read :doc:`django:howto/deployment/index`.

The site builds the URL of each resource from the request, with
:meth:`~django.http.HttpRequest.build_absolute_uri`. It takes the host of the request, its
scheme, and the script prefix of the project.

Trust the headers of the proxy
------------------------------

1. Add the host the clients use to :setting:`ALLOWED_HOSTS`.
2. If the proxy sends the original host in ``X-Forwarded-Host``, set :setting:`USE_X_FORWARDED_HOST`
   to ``True``.
3. If the proxy terminates HTTPS, set :setting:`SECURE_PROXY_SSL_HEADER` to the header and
   value the proxy sends, such as ``("HTTP_X_FORWARDED_PROTO", "https")``.
4. If the project lives under a path of the host, set :setting:`FORCE_SCRIPT_NAME`, or have the
   server pass ``SCRIPT_NAME``.

Make the proxy overwrite these headers on every request. With these settings, Django trusts the
headers, whoever sent them.

Check the URLs
--------------

Create a resource through the proxy, and read the :mdn:`Location` header of the response. It
starts with the public URL of the SCIM root, such as
``https://scim.example.org/app/scim/v2/Users/``.

Set the URLs without the request
--------------------------------

To build the URLs from a fixed base rather than from the request, subclass
:class:`~scim2_server.service.ScimService`, override its
:meth:`~scim2_server.service.ScimService.resource_location` method, and pass an instance as the
``service`` parameter of :class:`~scim2_django.ScimSite`.
:doc:`scim2_server:how-to/publish-the-resources-at-other-urls` describes this method.
