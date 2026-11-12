import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from abrl import lab


def _client(**kwargs):
    app = FastAPI()
    app.include_router(lab.router)
    return TestClient(app, **kwargs)


def test_lab_form_and_repeat_scenarios():
    with _client() as client:
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


def test_manual_stays_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ABRL_MANUAL_ORIGIN", raising=False)
    with _client() as client:
        assert "adsco.re" not in client.get("/lab/manual").text
        assert client.post("/lab/manual/start").status_code == 503


def test_manual_observation_requires_consent_and_one_matching_session(monkeypatch):
    base_url = "https://diagnostic.example"
    operator_token = "operator-token-for-test"
    monkeypatch.setenv("ABRL_MANUAL_ORIGIN", base_url)
    monkeypatch.setenv("ABRL_ADSCORE_ZONE_KEY", "test_zone_key")
    monkeypatch.setenv("ABRL_ADSCORE_ZONE_ID", "41")
    monkeypatch.setenv("ABRL_VERIFIER_TOKEN", "t" * 40)
    monkeypatch.setattr(lab, "OPERATOR_TOKEN", operator_token)
    monkeypatch.setattr(lab, "_next_manual_start", 0)
    monkeypatch.setattr(lab, "_pending", {})
    stored = []

    async def ready():
        return {"state": "pending_smoke_test"}

    async def verify(**metadata):
        assert metadata == {
            "signature": "fixture",
            "zone_id": "41",
            "ip_address": "8.8.8.8",
            "user_agent": "Lab test",
        }
        return {"verification_status": "verified", "verdict": "bot", "raw_code": 9, "reason": None}

    monkeypatch.setattr(lab, "diagnostics_status", ready)
    monkeypatch.setattr(lab, "verify_observation", verify)
    monkeypatch.setattr(lab, "_persist_manual", stored.append)

    headers = {"Origin": base_url, "User-Agent": "Lab test", "X-Forwarded-For": "1.1.1.1"}
    with _client(base_url=base_url, client=("8.8.8.8", 12345)) as client:
        denied = client.post("/lab/manual/start", data={"token": operator_token}, headers=headers)
        assert denied.status_code == 400
        start = client.post(
            "/lab/manual/start",
            data={"token": operator_token, "consent": "yes"},
            headers=headers,
        )
        assert start.status_code == 200
        assert operator_token not in start.text
        check_id = re.search(r'check_id: "([A-Za-z0-9_-]{43})"', start.text)
        assert check_id is not None
        observation = {"check_id": check_id[1], "signature": "fixture", "verdict": "clean"}
        wrong_agent = client.post(
            "/lab/manual/observation", json=observation, headers={**headers, "User-Agent": "Other"}
        )
        assert wrong_agent.status_code == 409
        verified = client.post("/lab/manual/observation", json=observation, headers=headers)
        assert verified.json() == {"verification_status": "verified"}
        assert stored[0]["verdict"] == "bot"
        repeated = client.post("/lab/manual/observation", json=observation, headers=headers)
        assert repeated.status_code == 409
        assert len(stored) == 1
