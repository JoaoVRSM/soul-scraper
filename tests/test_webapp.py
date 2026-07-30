from fastapi.testclient import TestClient

from soulscraper.webapp import app


client = TestClient(app)


def test_web_interface_and_assets_are_served() -> None:
    page = client.get("/")
    styles = client.get("/static/styles.css")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert "Até a alma do site" in page.text
    assert "Perfil de desempenho" in page.text
    assert 'id="browser-concurrency"' in page.text
    assert styles.status_code == 200
    assert "--green: #b8ff3d" in styles.text
    assert script.status_code == 200
    assert "resumeLastJob" in script.text
    assert "performanceProfiles" in script.text


def test_health_and_invalid_target_validation() -> None:
    assert client.get("/api/health").json()["status"] == "ok"

    response = client.post(
        "/api/jobs",
        json={
            "url": "example.com",
            "max_pages": 10,
            "max_depth": 2,
            "concurrency": 2,
        },
    )

    assert response.status_code == 422
    assert "http:// ou https://" in response.text
