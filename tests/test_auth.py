from datetime import UTC, datetime, timedelta

from app.auth.models import RefreshToken
from app.auth.security import hash_refresh_token

USER = {"email": "Jane@Example.com", "password": "secret123", "full_name": "Jane Doe"}


def register(client, **overrides):
    return client.post("/auth/register", json={**USER, **overrides})


def login(client, email=USER["email"], password=USER["password"]):
    return client.post("/auth/login", data={"username": email, "password": password})


def test_register(client):
    response = register(client)
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "jane@example.com"
    assert body["full_name"] == "Jane Doe"
    assert "password" not in body and "hashed_password" not in body


def test_register_duplicate_email(client):
    register(client)
    response = register(client, email="JANE@example.com")
    assert response.status_code == 409


def test_register_invalid_input(client):
    assert register(client, email="not-an-email").status_code == 422
    assert register(client, password="short").status_code == 422


def test_login_and_me(client):
    register(client)
    response = login(client)
    assert response.status_code == 200
    token = response.json()["access_token"]

    response = client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["email"] == "jane@example.com"


def test_login_wrong_password(client):
    register(client)
    assert login(client, password="wrong-password").status_code == 401


def test_login_unknown_email(client):
    assert login(client, email="nobody@example.com").status_code == 401


def test_me_requires_token(client):
    assert client.get("/users/me").status_code == 401


def test_me_invalid_token(client):
    response = client.get("/users/me", headers={"Authorization": "Bearer garbage"})
    assert response.status_code == 401


def test_refresh_returns_new_working_tokens(client):
    register(client)
    tokens = login(client).json()

    response = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 200
    new_tokens = response.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    me = client.get("/users/me", headers={"Authorization": f"Bearer {new_tokens['access_token']}"})
    assert me.status_code == 200


def test_refresh_token_is_single_use(client):
    register(client)
    refresh_token = login(client).json()["refresh_token"]

    assert client.post("/auth/refresh", json={"refresh_token": refresh_token}).status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401


def test_refresh_invalid_token(client):
    assert client.post("/auth/refresh", json={"refresh_token": "garbage"}).status_code == 401


def test_refresh_expired_token(client):
    user_id = register(client).json()["id"]

    async def insert_expired():
        await RefreshToken(
            token_hash=hash_refresh_token("expired-token"),
            user_id=user_id,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ).insert()

    client.portal.call(insert_expired)
    assert client.post("/auth/refresh", json={"refresh_token": "expired-token"}).status_code == 401


def test_logout_revokes_refresh_token(client):
    register(client)
    refresh_token = login(client).json()["refresh_token"]

    assert client.post("/auth/logout", json={"refresh_token": refresh_token}).status_code == 204
    assert client.post("/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401
