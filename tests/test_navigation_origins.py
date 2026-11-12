"""Every document origin must be separately authorized, resources never authorize it."""

import test_platform
from test_platform import client

from abrl import api as api_module

db = test_platform.db


def test_additional_navigation_origin_requires_its_own_marker(db, monkeypatch):
    monkeypatch.setattr(api_module, "Session", db)
    calls = []

    async def addresses(host):
        calls.append(host)
        return ["93.184.215.14"]

    monkeypatch.setattr(api_module, "resolve_public_ipv4", addresses)
    marker = {"value": b"wrong"}
    monkeypatch.setattr(api_module, "fetch_proof", lambda host, address: marker["value"])
    with client() as api:
        result = api.post(
            "/api/v1/targets",
            json={
                "name": "Two authorized origins",
                "authorized": True,
                "origin": "http://127.0.0.1:8400",
                "navigation_origins": ["https://owned.example"],
                "resource_origins": ["https://cdn.example"],
            },
        )
        assert result.status_code == 201, result.text
        target = result.json()
        assert target["verified"] is False and target["proof_token"]
        url = f"/api/v1/targets/{target['id']}/verify"
        assert api.post(url).status_code == 422
        marker["value"] = target["proof_token"].encode()
        assert api.post(url).json()["verified"] is True
        assert calls == ["owned.example", "owned.example"]
        # A version edit requires proving the entire updated navigation set again.
        result = api.put(
            f"/api/v1/targets/{target['id']}",
            json={
                "name": "Changed origins",
                "authorized": True,
                "origin": "http://127.0.0.1:8400",
                "navigation_origins": ["https://second.example"],
            },
        )
        assert result.json()["verified"] is False
        assert api.post(url).status_code == 422
