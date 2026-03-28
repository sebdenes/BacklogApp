"""Tests for Cortex server."""
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def tmp_data_dir(monkeypatch, tmp_path):
    """Use a temporary directory for all data files."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BACKLOG_API_KEY", raising=False)
    monkeypatch.delenv("BACKLOG_WEBHOOK_SECRET", raising=False)
    # Re-import to pick up new env
    import importlib
    import server
    importlib.reload(server)
    return tmp_path


@pytest.fixture
def client(tmp_data_dir):
    import server
    return TestClient(server.app)


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_root_redirects(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307)
    assert "/static/backlog.html" in resp.headers["location"]


def test_backlog_crud(client):
    # Initially 404
    resp = client.get("/api/backlog")
    assert resp.status_code == 404

    # Save
    projects = [{"id": "p1", "name": "Test", "lanes": []}]
    resp = client.post("/api/backlog", json=projects)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Read back
    resp = client.get("/api/backlog")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["name"] == "Test"


def test_inbox_disabled_without_secret(client):
    resp = client.post("/api/backlog/inbox", json={"items": [{"title": "test"}]})
    assert resp.status_code == 404


def test_inbox_with_secret(client, monkeypatch, tmp_data_dir):
    monkeypatch.setenv("BACKLOG_WEBHOOK_SECRET", "test-secret")
    import importlib
    import server
    importlib.reload(server)
    c = TestClient(server.app)

    # Post item
    resp = c.post(
        "/api/backlog/inbox",
        json={"items": [{"title": "Fix login bug", "priority": "p1"}]},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 1

    # Get items
    resp = c.get("/api/backlog/inbox")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    # Ack items
    resp = c.get("/api/backlog/inbox?ack=true")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    # Items cleared
    resp = c.get("/api/backlog/inbox")
    assert resp.json()["count"] == 0


def test_inbox_bad_auth(client, monkeypatch, tmp_data_dir):
    monkeypatch.setenv("BACKLOG_WEBHOOK_SECRET", "real-secret")
    import importlib
    import server
    importlib.reload(server)
    c = TestClient(server.app)

    resp = c.post(
        "/api/backlog/inbox",
        json={"items": [{"title": "test"}]},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status_code == 401


# ── Meetings tests ───────────────────────────────────────────────────────────

def test_meetings_empty(client):
    resp = client.get("/api/meetings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["meetings"] == []
    assert data["count"] == 0


def test_meetings_crud(client):
    # Save meetings
    meetings = [
        {"id": "mtg-001", "title": "Standup", "transcript": "discussed bugs"},
        {"id": "mtg-002", "title": "Retro", "transcript": "went well"},
    ]
    resp = client.post("/api/meetings", json={"meetings": meetings})
    assert resp.status_code == 200

    # List all
    resp = client.get("/api/meetings")
    assert resp.json()["count"] == 2

    # Get single
    resp = client.get("/api/meetings/mtg-001")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Standup"

    # Get missing
    resp = client.get("/api/meetings/mtg-999")
    assert resp.status_code == 404

    # Delete
    resp = client.delete("/api/meetings/mtg-001")
    assert resp.status_code == 200

    # Verify deleted
    resp = client.get("/api/meetings")
    assert resp.json()["count"] == 1
    assert resp.json()["meetings"][0]["id"] == "mtg-002"

    # Delete missing
    resp = client.delete("/api/meetings/mtg-999")
    assert resp.status_code == 404


def test_meetings_inbox_disabled_without_secret(client):
    resp = client.post(
        "/api/meetings/inbox",
        json={"title": "Test", "transcript": "hello"},
    )
    assert resp.status_code == 404


def test_meetings_inbox_with_secret(client, monkeypatch, tmp_data_dir):
    monkeypatch.setenv("BACKLOG_WEBHOOK_SECRET", "test-secret")
    import importlib
    import server
    importlib.reload(server)
    c = TestClient(server.app)

    resp = c.post(
        "/api/meetings/inbox",
        json={"title": "Sprint Planning", "transcript": "We need to ship by Friday"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "meeting_id" in data

    # Verify it was saved
    resp = c.get("/api/meetings")
    assert resp.json()["count"] == 1
    assert resp.json()["meetings"][0]["title"] == "Sprint Planning"


def test_meetings_inbox_bad_auth(client, monkeypatch, tmp_data_dir):
    monkeypatch.setenv("BACKLOG_WEBHOOK_SECRET", "real-secret")
    import importlib
    import server
    importlib.reload(server)
    c = TestClient(server.app)

    resp = c.post(
        "/api/meetings/inbox",
        json={"title": "Test", "transcript": "hello"},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status_code == 401


def test_extract_no_transcript(client):
    # Save a meeting with empty transcript
    client.post("/api/meetings", json={"meetings": [
        {"id": "mtg-empty", "title": "Empty", "transcript": ""},
    ]})
    resp = client.post("/api/meetings/mtg-empty/extract")
    assert resp.status_code == 400


def test_extract_meeting_not_found(client):
    resp = client.post("/api/meetings/mtg-missing/extract")
    assert resp.status_code == 404
