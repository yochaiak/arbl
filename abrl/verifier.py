"""Client for the internal AdScore verification service."""

import os
from urllib.parse import urlsplit

import httpx

_VERDICTS = {"clean", "potentially_unwanted", "proxy", "bot", "unknown"}


def _verifier_config() -> tuple[str, str] | None:
    url = os.getenv("ABRL_VERIFIER_URL", "http://127.0.0.1:8001").rstrip("/")
    token = os.getenv("ABRL_VERIFIER_TOKEN", "")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme == "http"
            and parts.hostname == "127.0.0.1"
            and parts.port is not None
            and not any((parts.username, parts.password, parts.path, parts.query, parts.fragment))
            and len(token) >= 32
        ):
            return url, token
    except ValueError:
        pass
    return None


def _unavailable(reason: str) -> dict:
    return {"verification_status": "unavailable", "verdict": None, "raw_code": None, "reason": reason}


async def verify_observation(*, signature: str, ip_address: str, user_agent: str, zone_id: str) -> dict:
    config = _verifier_config()
    if config is None:
        return _unavailable("missing_configuration")
    url, token = config
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(
                f"{url}/verify",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "signature": signature,
                    "ip_address": ip_address,
                    "user_agent": user_agent,
                    "zone_id": zone_id,
                },
            )
        if response.status_code != 200:
            return _unavailable("verifier_unavailable")
        result = response.json()
        if not isinstance(result, dict):
            return _unavailable("invalid_verifier_response")
        status = result.get("verification_status")
        if (
            status == "verified"
            and result.get("verdict") in _VERDICTS
            and type(result.get("raw_code")) is int
        ):
            return {
                "verification_status": "verified",
                "verdict": result["verdict"],
                "raw_code": result["raw_code"],
                "reason": None,
            }
        if status == "failed":
            return {
                "verification_status": "failed",
                "verdict": None,
                "raw_code": None,
                "reason": "signature_verification_failed",
            }
    except (httpx.HTTPError, ValueError, TypeError):
        return _unavailable("verifier_unavailable")
    return _unavailable("invalid_verifier_response")


async def diagnostics_status() -> dict:
    config = _verifier_config()
    if config is None:
        return {"state": "not_configured", "reason": "missing_configuration", "last_verified_at": None}
    url, token = config
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            response = await client.get(f"{url}/health", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json()
        if data.get("state") in {"not_configured", "pending_smoke_test", "ready"}:
            return {key: data.get(key) for key in ("state", "reason", "last_verified_at")}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass
    return {"state": "unavailable", "reason": "verifier_unavailable", "last_verified_at": None}
