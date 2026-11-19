from django.db import transaction
from scim2_server.memory import InMemoryStorage


class AtomicStorage(InMemoryStorage):
    def operation(self):
        return transaction.atomic()
