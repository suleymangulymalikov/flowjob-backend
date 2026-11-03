import pytest

from app.cv.render import format_dates

USER = {"email": "jane@example.com", "password": "secret123", "full_name": "Jane Doe"}


@pytest.fixture
def headers(client):
    client.post("/auth/register", json=USER)
    tokens = client.post("/auth/login", data={"username": USER["email"], "password": USER["password"]}).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_format_dates():
    assert format_dates("2023-01", "2024-06") == "Jan 2023 – Jun 2024"
    assert format_dates("2023-01", None) == "Jan 2023 – Present"


def test_cv_pdf_requires_auth(client):
    assert client.get("/cv/pdf").status_code == 401


def test_cv_pdf_with_empty_profile(client, headers):
    response = client.get("/cv/pdf", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_cv_pdf_with_profile(client, headers):
    client.patch(
        "/profile",
        json={
            "personal": {"location": "Berlin"},
            "experience": [
                {"company": "Acme", "position": "Developer", "start": "2023-01", "bullets": ["Built APIs"]}
            ],
            "skills": ["Python"],
            "languages": [{"name": "German", "level": "B2"}],
        },
        headers=headers,
    )
    response = client.get("/cv/pdf", headers=headers)
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert 'filename="Jane_Doe_CV.pdf"' in response.headers["content-disposition"]
