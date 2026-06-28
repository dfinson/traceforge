"""Integration tests for the Issue Tracker API endpoints."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthcheck() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_say_hello() -> None:
    response = client.get("/hello")

    assert response.status_code == 200
    assert response.json() == {"message": "Hello, World!"}


def test_get_joke() -> None:
    response = client.get("/joke")

    assert response.status_code == 200
    assert "joke" in response.json()


def test_create_ticket_returns_created_record() -> None:
    response = client.post(
        "/tickets",
        json={
            "title": "Broken mobile nav",
            "description": "Nav drawer overlays the footer on narrow screens.",
            "customer_email": "ops@example.com",
            "priority": "high",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Broken mobile nav"
    assert body["priority"] == "high"


def test_list_tickets_excludes_archived_by_default() -> None:
    response = client.get("/tickets")

    assert response.status_code == 200
    body = response.json()
    assert all(item["archived"] is False for item in body["items"])


def test_list_tickets_includes_archived_when_requested() -> None:
    response = client.get("/tickets", params={"include_archived": True})

    assert response.status_code == 200
    body = response.json()
    archived_items = [item for item in body["items"] if item["archived"]]
    assert len(archived_items) >= 1


def test_list_tickets_supports_search() -> None:
    response = client.get("/tickets", params={"search": "csv"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Exported CSV omits assignee"


def test_search_excludes_archived_by_default() -> None:
    response = client.get("/tickets", params={"search": "webhook"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0


def test_search_includes_archived_when_requested() -> None:
    response = client.get("/tickets", params={"search": "webhook", "include_archived": True})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["archived"] is True
