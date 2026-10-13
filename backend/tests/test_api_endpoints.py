"""
tests/test_api_endpoints.py — API integration tests for Phase 1 + Phase 2 endpoints.

Tests:
  - GET /reports (Phase 1)
  - GET /sources (Phase 2)
  - GET /sources/99999/documents (Phase 2 404)
  - GET /sources/99999/quality (Phase 2 404)
  - GET /documents/99999 (Phase 2 404)
  - POST /upload with CSV (Phase 1 endpoint + Phase 2 backfill verification)
  - POST /ingest/url with SSRF blocked target (Phase 2 422)
  - POST /ingest/url with bad scheme (Phase 2 422)
"""

import io
import pytest
from fastapi.testclient import TestClient
from main import app
from db import init_db

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def setup_database():
    init_db()


def test_get_reports_phase1():
    response = client.get("/reports")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_get_sources_empty_or_list():
    response = client.get("/sources")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_get_documents_nonexistent_source():
    response = client.get("/sources/999999/documents")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_get_quality_nonexistent_source():
    response = client.get("/sources/999999/quality")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_get_document_nonexistent():
    response = client.get("/documents/999999")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_post_upload_csv_persists_source_and_records():
    csv_content = b"name,age,city\nAlice,30,Wonderland\nBob,25,Builder\nCharlie,35,Chocolate"
    files = {"file": ("test_people.csv", io.BytesIO(csv_content), "text/csv")}
    
    response = client.post("/upload", files=files)
    assert response.status_code == 200
    data = response.json()
    
    # Phase 1 contract fields
    assert "file_id" in data
    assert data["filename"] == "test_people.csv"
    assert data["format"] == "csv"
    assert "quality_report" in data
    
    # Phase 2 addition
    assert "source_id" in data
    source_id = data["source_id"]
    assert source_id is not None
    
    # Check that GET /sources lists this source
    sources_resp = client.get("/sources")
    assert sources_resp.status_code == 200
    source_ids = [s["id"] for s in sources_resp.json()]
    assert source_id in source_ids


def test_post_upload_unsupported_extension():
    files = {"file": ("test.exe", io.BytesIO(b"binary"), "application/octet-stream")}
    response = client.post("/upload", files=files)
    assert response.status_code == 415


def test_post_ingest_url_ssrf_blocked():
    # Loopback or metadata IP should be rejected with 422
    payload = {"url": "http://169.254.169.254/latest/meta-data/", "max_pages": 5}
    response = client.post("/ingest/url", json=payload)
    assert response.status_code == 422
    assert "link-local" in response.json()["detail"].lower() or "blocked" in response.json()["detail"].lower()


def test_post_ingest_url_unsupported_scheme():
    payload = {"url": "ftp://example.com/file.txt", "max_pages": 5}
    response = client.post("/ingest/url", json=payload)
    assert response.status_code == 422
    assert "scheme" in response.json()["detail"].lower()
