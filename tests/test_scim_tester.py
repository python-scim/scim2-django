import pytest
from django.core.signals import request_finished
from django.core.signals import request_started
from django.core.wsgi import get_wsgi_application
from django.db import close_old_connections
from scim2_client.engines.wsgi import WSGISCIMClient
from scim2_tester.testing import ScimServerContract


@pytest.mark.django_db
class TestScimServer(ScimServerContract):
    @pytest.fixture
    def scim_client(self):
        client = WSGISCIMClient(
            get_wsgi_application(), base_url="http://localhost/scim/v2"
        )
        request_started.disconnect(close_old_connections)
        request_finished.disconnect(close_old_connections)
        yield client
        request_started.connect(close_old_connections)
        request_finished.connect(close_old_connections)
