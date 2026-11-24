from browser_fixture import create_app
from fastapi.testclient import TestClient

from abrl.api import app


def test_lab_form_and_repeat_scenarios():
    with TestClient(create_app()) as client:
        assert 'id="lab-ready"' in client.get("/lab").text
        assert 'id="interaction-complete"' in client.get("/lab/interaction").text
        assert 'id="journey-complete"' in client.get("/lab/finish").text
        rejected = client.post("/lab/form", data={"name": "", "topic": "research"})
        assert rejected.status_code == 422
        submitted = client.post("/lab/form", data={"name": "<sample>", "topic": "testing"})
        assert 'id="form-success"' in submitted.text
        assert "<sample>" not in submitted.text
        assert 'id="visit-count">1<' in client.get("/lab/repeat").text
        assert 'id="visit-count">2<' in client.get("/lab/repeat").text


def test_production_workspace_retires_lab_and_research_routes():
    with TestClient(app) as client:
        for method, path in (
            ("GET", "/lab"),
            ("GET", "/lab/manual"),
            ("POST", "/lab/research"),
            ("POST", "/api/v1/starter"),
            ("GET", "/api/v1/diagnostics"),
            ("GET", "/api/v1/experiments"),
            ("GET", "/api/v1/research/comparisons"),
            ("GET", "/api/v1/research/export"),
        ):
            assert client.request(method, path).status_code == 404, path
    assert "/api/v1/integration-observations" in app.openapi()["paths"]
