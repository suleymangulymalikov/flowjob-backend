USER = {"email": "jane@example.com", "password": "secret123", "full_name": "Jane Doe"}


def auth_headers(client, email=USER["email"], password=USER["password"]):
    tokens = client.post("/auth/login", data={"username": email, "password": password}).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_update_full_name(client):
    client.post("/auth/register", json=USER)
    response = client.patch("/users/me", json={"full_name": "Jane Smith"}, headers=auth_headers(client))
    assert response.status_code == 200
    assert response.json()["full_name"] == "Jane Smith"


def test_update_email_requires_current_password(client):
    client.post("/auth/register", json=USER)
    headers = auth_headers(client)

    response = client.patch("/users/me", json={"email": "new@example.com"}, headers=headers)
    assert response.status_code == 403

    response = client.patch(
        "/users/me",
        json={"email": "new@example.com", "current_password": "wrong-password"},
        headers=headers,
    )
    assert response.status_code == 403


def test_update_email(client):
    client.post("/auth/register", json=USER)
    response = client.patch(
        "/users/me",
        json={"email": "New@Example.com", "current_password": USER["password"]},
        headers=auth_headers(client),
    )
    assert response.status_code == 200
    assert response.json()["email"] == "new@example.com"
    # Can log in with the new email.
    login = client.post("/auth/login", data={"username": "new@example.com", "password": USER["password"]})
    assert login.status_code == 200


def test_update_email_taken(client):
    client.post("/auth/register", json=USER)
    client.post("/auth/register", json={**USER, "email": "other@example.com"})
    response = client.patch(
        "/users/me",
        json={"email": "other@example.com", "current_password": USER["password"]},
        headers=auth_headers(client),
    )
    assert response.status_code == 409


def test_change_password(client):
    client.post("/auth/register", json=USER)
    old_refresh = client.post(
        "/auth/login", data={"username": USER["email"], "password": USER["password"]}
    ).json()["refresh_token"]

    response = client.post(
        "/users/me/password",
        json={"current_password": USER["password"], "new_password": "new-secret123"},
        headers=auth_headers(client),
    )
    assert response.status_code == 200
    new_tokens = response.json()

    # Old password no longer works, new one does.
    assert client.post("/auth/login", data={"username": USER["email"], "password": USER["password"]}).status_code == 401
    assert client.post("/auth/login", data={"username": USER["email"], "password": "new-secret123"}).status_code == 200
    # Other sessions are logged out; the returned tokens work.
    assert client.post("/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}).status_code == 200


def test_change_password_wrong_current(client):
    client.post("/auth/register", json=USER)
    response = client.post(
        "/users/me/password",
        json={"current_password": "wrong-password", "new_password": "new-secret123"},
        headers=auth_headers(client),
    )
    assert response.status_code == 403
