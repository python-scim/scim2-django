Commit the changes
==================

This guide ties the changes of each SCIM operation to a database transaction. It addresses
developers who write a :class:`~scim2_server.storage.ScimStorage` upon the database of a Django
project. Writing the storage itself is covered by :doc:`scim2_server:how-to/write-a-storage`.

The site never opens a transaction, and never commits. It encloses each SCIM operation in the
:meth:`~scim2_server.storage.ScimStorage.operation` context manager of the storage. An
operation is a request, or one operation of a bulk request. When the operation fails, the
exception goes through the context manager.

Enclose each operation in a transaction
---------------------------------------

Return :func:`transaction.atomic() <django.db.transaction.atomic>` from the
:meth:`~scim2_server.storage.ScimStorage.operation` method of the storage:

.. literalinclude:: ../../examples/transactions.py
   :language: python
   :pyobject: AtomicStorage

A failed operation is then rolled back, and a successful one is committed. A failed operation
of a bulk request does not undo the others, per :rfc:`RFC 7644 §3.7 <7644#section-3.7>`.

For a project with several databases, pass the alias of the database to
:func:`~django.db.transaction.atomic`, such as ``transaction.atomic(using="users")``.

Keep ATOMIC_REQUESTS
--------------------

With :setting:`ATOMIC_REQUESTS <DATABASE-ATOMIC_REQUESTS>`, Django encloses each request in a
transaction, and commits it unless the view raises an exception. The site answers the SCIM
errors with a response, without raising: the transaction of the request is committed.

The transaction of each operation is then a savepoint in the transaction of the request. The
savepoint of a failed operation is rolled back, and the other changes are committed with the
request.
