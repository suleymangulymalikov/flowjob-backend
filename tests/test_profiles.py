import pytest

USER = {"email": "jane@example.com", "password": "secret123", "full_name": "Jane Doe"}

EXPERIENCE = {
    "company": "Acme",
    "position": "Backend Developer",
    "start": "2023-01",
    "end": None,
    "bullets": ["Built REST APIs with FastAPI", "Cut response times by 40%"],
}


@pytest.fixture
def headers(client):
    client.post("/auth/register", json=USER)
    tokens = client.post("/auth/login", data={"username": USER["email"], "password": USER["password"]}).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_profile_requires_auth(client):
    assert client.get("/profile").status_code == 401


def test_new_user_gets_empty_profile(client, headers):
    response = client.get("/profile", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["experience"] == [] and body["skills"] == []
    assert body["personal"]["headline"] is None


def test_patch_saves_section(client, headers):
    response = client.patch("/profile", json={"experience": [EXPERIENCE]}, headers=headers)
    assert response.status_code == 200
    assert response.json()["experience"][0]["company"] == "Acme"

    assert client.get("/profile", headers=headers).json()["experience"][0]["bullets"] == EXPERIENCE["bullets"]


def test_patch_keeps_other_sections(client, headers):
    client.patch("/profile", json={"skills": ["Python", "FastAPI"]}, headers=headers)
    client.patch("/profile", json={"personal": {"headline": "Backend Developer"}}, headers=headers)

    body = client.get("/profile", headers=headers).json()
    assert body["skills"] == ["Python", "FastAPI"]
    assert body["personal"]["headline"] == "Backend Developer"


def test_patch_replaces_whole_section(client, headers):
    client.patch("/profile", json={"skills": ["Python", "Java"]}, headers=headers)
    client.patch("/profile", json={"skills": ["Go"]}, headers=headers)
    assert client.get("/profile", headers=headers).json()["skills"] == ["Go"]


def test_patch_null_section_is_ignored(client, headers):
    client.patch("/profile", json={"skills": ["Python"]}, headers=headers)
    client.patch("/profile", json={"skills": None}, headers=headers)
    assert client.get("/profile", headers=headers).json()["skills"] == ["Python"]


def test_invalid_input_rejected(client, headers):
    bad_date = {**EXPERIENCE, "start": "2023-13"}
    assert client.patch("/profile", json={"experience": [bad_date]}, headers=headers).status_code == 422

    bad_level = {"name": "German", "level": "fluent"}
    assert client.patch("/profile", json={"languages": [bad_level]}, headers=headers).status_code == 422


def test_profiles_are_per_user(client, headers):
    client.patch("/profile", json={"skills": ["Python"]}, headers=headers)

    other = {**USER, "email": "other@example.com"}
    client.post("/auth/register", json=other)
    tokens = client.post("/auth/login", data={"username": other["email"], "password": other["password"]}).json()
    other_headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert client.get("/profile", headers=other_headers).json()["skills"] == []
