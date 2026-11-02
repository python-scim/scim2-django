SCIM_JSON = "application/scim+json"
ENTERPRISE_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


def test_example_server_creates_and_reads_a_user(client):
    """Create a user with the enterprise extension on the example server, and read it back."""
    user_payload = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User", ENTERPRISE_SCHEMA],
        "userName": "bjensen",
        ENTERPRISE_SCHEMA: {"employeeNumber": "42"},
    }

    response = client.post("/scim/v2/Users", user_payload, content_type=SCIM_JSON)
    assert response.status_code == 201
    user = response.json()
    assert user["userName"] == "bjensen"
    assert user[ENTERPRISE_SCHEMA] == {"employeeNumber": "42"}
    assert user["meta"]["resourceType"] == "User"
    assert user["meta"]["location"].endswith(f"/scim/v2/Users/{user['id']}")

    response = client.get(user["meta"]["location"])
    assert response.status_code == 200
    assert response.json() == user


def test_example_authentication_refuses_clients_without_token(client):
    response = client.get("/protected/scim/v2/Users")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="SCIM"'
    assert response.json()["detail"] == "Missing or invalid token"


def test_example_authentication_accepts_clients_with_a_known_token(client):
    response = client.get(
        "/protected/scim/v2/Users", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200


def test_example_authentication_leaves_the_service_provider_config_open(client):
    response = client.get("/protected/scim/v2/ServiceProviderConfig")

    assert response.status_code == 200
