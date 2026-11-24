"""Public proxy registration through durable admission and provider binding, without network calls."""

import base64

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from test_platform import client, create, data
from test_platform import db as db

from abrl import platform_service, session_runner
from abrl.browser import BrowserResult
from abrl.db import TargetVersion
from abrl.platform_models import BrowserSession, ProxyInventory, ProxyLease, SessionAttempt, SessionEvent
from abrl.platform_schemas import BatchInput, ProxyInventoryInput, ProxyPolicyInput
from abrl.proxy import DecodoBinding, OxylabsBinding, ProxyBinding, lease_proxy_config, select_proxy


@pytest.fixture
def proxy_runtime(monkeypatch):
    monkeypatch.setattr(
        platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
    )
    monkeypatch.setenv("ABRL_SECRET_PROXY_USER", "customer-authorized")
    monkeypatch.setenv("ABRL_SECRET_PROXY_PASSWORD", "private-provider-password")
    monkeypatch.setenv("ABRL_SECRET_DECODO_USER", "testuser01")
    monkeypatch.setenv("ABRL_SECRET_DECODO_PASSWORD", "test+secret")


SCOPED_ORIGIN = "https://authorized.example"
OTHER_ORIGIN = "https://other.example"


def register_policy(api, target_origin=None):
    response = api.post(
        "/api/v1/proxy-policies",
        json={"name": "Proxy policy", "country": "IL", "target_origin": target_origin},
    )
    assert response.status_code == 201, response.text
    return response.json()


def register_inventory(api, target_origin):
    response = api.post(
        "/api/v1/proxy-inventory",
        json={
            "name": "Proxy inventory",
            "product": "datacenter",
            "country": "IL",
            "username_ref": "PROXY_USER",
            "password_ref": "PROXY_PASSWORD",
            "target_origin": target_origin,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_batch_preflight_reads_decodo_inventory_without_reserving_it(db, proxy_runtime):
    with client() as api:
        inventory = api.post("/api/v1/proxy-inventory", json={
            "name": "Decodo test inventory", "provider": "decodo",
            "username_ref": "DECODO_USER", "password_ref": "DECODO_PASSWORD", "capacity": 1,
        })
        assert inventory.status_code == 201, inventory.text
        inventory_id = inventory.json()["id"]
        policy = api.post("/api/v1/proxy-policies", json={
            "name": "Decodo preflight", "provider": "decodo", "continuity": "sticky",
            "continuity_minutes": 10, "provider_resolved_ack": True,
        })
        assert policy.status_code == 201, policy.text
        payload = data(proxy_policy_id=policy.json()["id"]).model_dump(mode="json")
        origin = "http://localhost:8430"
        endpoint = "/api/v1/batches/preflight"
        response = api.post(endpoint, json=payload, headers={"Origin": origin})
        assert response.status_code == 200, response.text
        assert response.headers["access-control-allow-origin"] == origin
        assert "proxy_capacity" not in {notice["code"] for notice in response.json()["notices"]}

        with db.begin() as connection:
            row = connection.get(ProxyInventory, inventory_id)
            assert row.active_leases == 0
            assert connection.scalar(select(ProxyLease)) is None
            assert connection.scalar(select(BrowserSession)) is None
            row.active_leases = 1

        response = api.post(endpoint, json=payload)
        assert response.status_code == 200, response.text
        assert "proxy_capacity" in {notice["code"] for notice in response.json()["notices"]}


@pytest.mark.parametrize("timeout", [99, 120_001, 17500.5, "17500", True])
def test_proxy_policy_navigation_timeout_requires_bounded_integer_milliseconds(timeout):
    with pytest.raises(ValueError, match="initial_navigation_timeout_ms"):
        ProxyPolicyInput(name="Proxy policy", initial_navigation_timeout_ms=timeout)


def test_proxy_policy_navigation_timeout_is_versioned_and_pinned(db, proxy_runtime):
    config = {
        "name": "Decodo navigation deadline",
        "provider": "decodo",
        "continuity": "sticky",
        "provider_resolved_ack": True,
    }
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": SCOPED_ORIGIN}
    with client() as api:
        response = api.post("/api/v1/proxy-inventory", json={
            "name": "Fixture inventory", "provider": "decodo",
            "username_ref": "DECODO_USER", "password_ref": "DECODO_PASSWORD",
        })
        assert response.status_code == 201, response.text
        response = api.post("/api/v1/proxy-policies", json={**config, "initial_navigation_timeout_ms": 17_500})
        assert response.status_code == 201, response.text
        policy = response.json()
        assert policy["initial_navigation_timeout_ms"] == 17_500 and policy["version"] == 1

        create(db, mode="live", proxy_policy_id=policy["id"])
        path = f"/api/v1/proxy-policies/{policy['id']}"
        response = api.put(path, json={**config, "initial_navigation_timeout_ms": 23_000})
        assert response.status_code == 200, response.text
        assert response.json()["initial_navigation_timeout_ms"] == 23_000

        response = api.put(path, json={**config, "initial_navigation_timeout_ms": None})
        assert response.status_code == 200, response.text
        assert response.json()["initial_navigation_timeout_ms"] is None
        response = api.get("/api/v1/proxy-policies")
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["initial_navigation_timeout_ms"] is None
        response = api.get(f"{path}/versions")
        assert response.status_code == 200, response.text
        assert [(revision["version"], revision["initial_navigation_timeout_ms"]) for revision in response.json()] == [
            (3, None), (2, 23_000), (1, 17_500),
        ]

    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None and work.proxy["provider"] == "decodo"
    assert work.config["proxy_policy"]["version"] == 1
    assert work.config["proxy_policy"]["initial_navigation_timeout_ms"] == 17_500


@pytest.mark.parametrize(
    "unexpected_scope",
    [
        {"origin": OTHER_ORIGIN},
        {"navigation_origins": [OTHER_ORIGIN]},
        {"resource_origins": [OTHER_ORIGIN]},
    ],
)
def test_scoped_policy_rejects_unpinned_and_pinned_batches(db, unexpected_scope):
    with client() as api:
        policy = register_policy(api, "HTTPS://Authorized.Example:443/")
        assert policy["target_origin"] == SCOPED_ORIGIN
        settings = data(mode="live", proxy_policy_id=policy["id"])
        with db.begin() as connection:
            target = connection.get(TargetVersion, ("target", 1))
            target.config = {**target.config, "origin": SCOPED_ORIGIN}
            pinned = platform_service.batch_snapshot(connection, settings, "default")
            target.config = {**target.config, **unexpected_scope}
        response = api.post("/api/v1/batches", json=settings.model_dump(mode="json"))
        assert response.status_code == 409
        assert "sole site origin" in response.json()["detail"]

    pinned["target"] = {**pinned["target"], **unexpected_scope}
    with db.begin() as connection, pytest.raises(HTTPException, match="sole site origin"):
        platform_service.create_batch(connection, settings, "default", pinned=pinned)


@pytest.mark.parametrize(
    "unexpected_scope",
    [
        {"origin": OTHER_ORIGIN},
        {"navigation_origins": [OTHER_ORIGIN]},
        {"resource_origins": [OTHER_ORIGIN]},
    ],
)
def test_scoped_inventory_is_ineligible_even_under_unscoped_policy(db, proxy_runtime, unexpected_scope):
    with client() as api:
        inventory = register_inventory(api, "HTTPS://Authorized.Example:443/")
        assert inventory["target_origin"] == SCOPED_ORIGIN
        policy = register_policy(api)
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": SCOPED_ORIGIN, **unexpected_scope}
    create(db, mode="live", proxy_policy_id=policy["id"])
    with db.begin() as connection:
        session_runner.admission_tick(connection)
        assert not connection.scalar(select(BrowserSession)).admitted
    assert session_runner.claim("standard") is None


def test_claim_rechecks_inventory_scope_after_admission(db, proxy_runtime):
    with client() as api:
        inventory = register_inventory(api, SCOPED_ORIGIN)
        policy = register_policy(api, SCOPED_ORIGIN)
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": SCOPED_ORIGIN}
    create(db, mode="live", proxy_policy_id=policy["id"])
    with db.begin() as connection:
        session_runner.admission_tick(connection)
        assert connection.scalar(select(BrowserSession)).admitted
        row = connection.get(ProxyInventory, inventory["id"])
        row.config = {**row.config, "target_origin": OTHER_ORIGIN}
    assert session_runner.claim("standard") is None
    with db.begin() as connection:
        assert connection.scalar(select(ProxyLease)) is None
        row = connection.get(ProxyInventory, inventory["id"])
        row.config = {**row.config, "target_origin": SCOPED_ORIGIN}
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None
    assert work.proxy["target_origin"] == SCOPED_ORIGIN


@pytest.mark.parametrize("target_origin", ["http://127.0.0.1:8400", "https://authorized.example/location"])
def test_proxy_target_scope_requires_an_https_origin(target_origin):
    with pytest.raises(ValueError, match="origin"):
        ProxyPolicyInput(name="Proxy policy", target_origin=target_origin)
    with pytest.raises(ValueError, match="origin"):
        ProxyInventoryInput(
            name="Proxy inventory",
            product="residential",
            country="IL",
            username_ref="PROXY_USER",
            password_ref="PROXY_PASSWORD",
            target_origin=target_origin,
        )


def register_and_claim(
    db, *, continuity="strict", product="residential", duration=45, connection_load=1, provider="oxylabs"
):
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": "https://authorized.example"}
    if provider == "decodo":
        inventory_config = {
            "provider": "decodo", "username_ref": "DECODO_USER", "password_ref": "DECODO_PASSWORD",
        }
        policy_config = {"provider": "decodo"}
    else:
        inventory_config = {
            "product": product,
            "username_ref": "PROXY_USER",
            "password_ref": "PROXY_PASSWORD",
            "country": "IL",
            "exclusive": product.startswith("dedicated_"),
        }
        policy_config = {"country": "IL", "products": [product]}
    with client() as api:
        response = api.post(
            "/api/v1/proxy-inventory",
            json={
                "name": "Authorized test inventory",
                **inventory_config,
                "capacity": max(2, connection_load),
                "max_duration_minutes": 60,
            },
        )
        assert response.status_code == 201, response.text
        inventory_id = response.json()["id"]
        response = api.post(
            "/api/v1/proxy-policies",
            json={
                "name": "Transport continuity",
                **policy_config,
                "continuity": continuity,
                "continuity_minutes": duration,
                "provider_resolved_ack": True,
                "connection_load": connection_load,
            },
        )
        assert response.status_code == 201, response.text
        policy_id = response.json()["id"]
    create(db, mode="live", proxy_policy_id=policy_id)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None
    return work, inventory_id


@pytest.mark.parametrize("load", [2, 100])
def test_policy_connection_load_reserves_and_releases_actual_broker_capacity(db, proxy_runtime, load):
    work, inventory_id = register_and_claim(db, connection_load=load)
    assert work.proxy["max_connections"] == load
    with db() as connection:
        assert connection.get(ProxyInventory, inventory_id).active_leases == load
    session_runner.finish(work, failed_transport(failure_kind="budget_exhausted"))
    with db() as connection:
        assert connection.get(ProxyInventory, inventory_id).active_leases == 0


@pytest.mark.parametrize("provider", ["oxylabs", "decodo"])
@pytest.mark.parametrize(("outcome", "check_error"), [("completed", False), ("completed", True), ("blocked", False)])
async def test_session_proxy_check_is_post_workflow_and_diagnostic_only(
    db, proxy_runtime, monkeypatch, tmp_path, provider, outcome, check_error
):
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path)
    work, _ = register_and_claim(
        db, provider=provider, continuity="sticky" if provider == "decodo" else "strict"
    )
    calls = []

    async def run_browser(**settings):
        assert settings["proxy"] is work.proxy
        calls.append("browser_closed")
        return BrowserResult(outcome, 10, None, {"runtime_stopped": True}, True)

    async def check(binding):
        assert calls == ["browser_closed"]
        assert binding.provider == provider
        assert binding.authorization() == ProxyBinding.from_config(work.proxy).authorization()
        calls.append("proxy_checked")
        if check_error:
            raise OSError("provider unavailable")
        return "8.8.4.4"

    async def preflight(binding, target_origin, policy, cancelled):
        assert provider == "decodo" and not calls
        return {"status": "partial", "blocked": False, "reason": "missing_evidence", "checks": {}, "timings_ms": {}}

    monkeypatch.setattr(session_runner, "run_decodo_preflight", preflight)
    monkeypatch.setattr(session_runner, "execute_workflow", run_browser)
    monkeypatch.setattr(session_runner, "check_proxy_ip", check)
    await session_runner.execute(work)

    expected_ip = "8.8.4.4" if outcome == "completed" and not check_error else None
    assert ("proxy_checked" in calls) == (outcome == "completed")
    with db() as connection:
        session = connection.get(BrowserSession, work.id)
        attempt = connection.get(SessionAttempt, work.lease)
        assert session.status == outcome
        assert attempt.proxy_check_ip == expected_ip
        assert "proxy_check_ip" not in session.metadata_json


@pytest.mark.parametrize("continuity,prefix", [("sticky", "sessid"), ("strict", "sessid_oneip")])
def test_api_residential_policy_reaches_exact_documented_gateway_binding(
    db, proxy_runtime, continuity, prefix
):
    work, identity = register_and_claim(db, continuity=continuity)
    assert work.proxy["ttl_minutes"] == 45
    assert work.proxy["strict_affinity"] is (continuity == "strict")
    assert work.proxy["session_key"]
    binding = OxylabsBinding.from_config(work.proxy)
    credentials = base64.b64decode(binding.authorization().split()[1]).decode()
    assert (
        credentials
        == f"customer-authorized-cc-IL-{prefix}-{work.proxy['session_key']}-sesstime-45:private-provider-password"
    )
    assert binding.public_metadata()["continuity"] == continuity
    assert "private-provider-password" not in str(binding.public_metadata())
    with db() as connection:
        lease = connection.scalar(select(ProxyLease))
        assert lease.config == work.proxy and lease.epoch == work.epoch
        assert lease.rationale["candidate"]["capabilities"]["supports_sticky"]
        assert connection.get(ProxyInventory, identity).active_leases == 1


def test_decodo_us_policy_pins_gateway_and_provider_across_admission_and_recovery(db, proxy_runtime):
    with client() as api:
        response = api.post(
            "/api/v1/proxy-inventory",
            json={
                "name": "Legacy US Residential", "product": "residential", "country": "US",
                "username_ref": "PROXY_USER", "password_ref": "PROXY_PASSWORD",
            },
        )
        assert response.status_code == 201, response.text
        oxylabs_id = response.json()["id"]

    work, inventory_id = register_and_claim(db, provider="decodo", continuity="sticky")
    assert work.config["proxy_policy"]["provider"] == "decodo"
    assert work.config["proxy_policy"]["products"] == ["residential"]
    assert work.config["proxy_policy"]["country"] == "US"
    assert (work.proxy["host"], work.proxy["port"], work.proxy["country"]) == ("gate.decodo.com", 7000, "US")
    assert work.proxy["ttl_minutes"] == 45
    assert not work.proxy["strict_affinity"]
    binding = ProxyBinding.from_config(work.proxy)
    assert isinstance(binding, DecodoBinding)
    credentials = base64.b64decode(binding.authorization().split()[1]).decode()
    assert credentials == (
        f"user-testuser01-country-us-session-{work.proxy['session_key']}-sessionduration-45:test+secret"
    )
    assert "test+secret" not in repr(binding)
    assert "testuser01" not in str(binding.public_metadata())

    session_runner.finish(work, failed_transport())
    with db.begin() as connection:
        assert connection.get(BrowserSession, work.id).status == "queued"
        lease = connection.scalar(select(ProxyLease))
        assert lease.inventory_id == inventory_id and lease.config == work.proxy
        connection.get(ProxyInventory, inventory_id).enabled = False
    assert permit_next_claim(db) is None
    with db() as connection:
        assert not connection.get(BrowserSession, work.id).admitted
        assert connection.get(ProxyInventory, oxylabs_id).active_leases == 0


@pytest.mark.parametrize("override", [{"country": "CA"}, {"product": "isp"}, {"port": 7001},
                                      {"host": "pr.oxylabs.io"}, {"exclusive": True}])
def test_decodo_inventory_rejects_unsupported_configuration(override):
    with pytest.raises(ValueError):
        ProxyInventoryInput.model_validate({
            "name": "Inventory", "provider": "decodo", "username_ref": "USER", "password_ref": "PASSWORD",
            **override,
        })


@pytest.mark.parametrize("override", [{"country": "CA"}, {"products": ["isp"]}, {"continuity": "strict"},
                                      {"allow_direct": True}, {"fallback_countries": ["CA"]}])
def test_decodo_policy_rejects_unsupported_configuration(override):
    with pytest.raises(ValueError):
        ProxyPolicyInput.model_validate({"name": "Policy", "provider": "decodo", **override})


def test_no_continuity_does_not_silently_make_a_sticky_residential_session(db, proxy_runtime):
    work, _ = register_and_claim(db, continuity="none")
    assert not {"session_key", "ttl_minutes", "strict_affinity"} & work.proxy.keys()
    binding = OxylabsBinding.from_config(work.proxy)
    assert b"sessid" not in base64.b64decode(binding.authorization().split()[1])
    assert binding.public_metadata()["continuity"] == "none"


@pytest.mark.parametrize(
    "product,host",
    [
        ("isp", "isp.oxylabs.io"),
        ("dedicated_isp", "disp.oxylabs.io"),
        ("datacenter", "dc.oxylabs.io"),
        ("dedicated_datacenter", "ddc.oxylabs.io"),
    ],
)
def test_fixed_gateway_defaults_are_product_specific_and_never_receive_residential_parameters(
    product, host
):
    config = ProxyInventoryInput(
        name="Inventory", product=product, username_ref="USER", password_ref="PASSWORD", country="IL"
    ).model_dump()
    assert config["host"] == host and config["port"] == 8001
    policy = ProxyPolicyInput(name="Fixed", products=[product]).model_dump()
    chosen = select_proxy(policy, [{**config, "id": "fixed"}])
    assert chosen["status"] == "selected"
    lease = lease_proxy_config(config, policy, "newSession")
    assert not {"session_key", "strict_affinity", "ttl_minutes"} & lease.keys()
    with pytest.raises(ValueError, match="rotating port 8000"):
        ProxyInventoryInput.model_validate({**config, "port": 8000})
    with pytest.raises(ValueError, match="product's assigned fixed port"):
        ProxyInventoryInput.model_validate({**config, "host": "pr.oxylabs.io", "port": 7777})


def test_residential_cannot_claim_fixed_or_exclusive_inventory():
    with pytest.raises(ValueError, match="fixed or exclusive"):
        ProxyPolicyInput(name="Invalid", products=["residential"], continuity="fixed")
    with pytest.raises(ValueError, match="Exclusive inventory"):
        ProxyInventoryInput(
            name="Invalid",
            product="residential",
            username_ref="USER",
            password_ref="PASSWORD",
            country="IL",
            exclusive=True,
        )


def failed_transport(**changes):
    return BrowserResult(
        "failed",
        100,
        "Transport setup failed",
        {
            "failure_kind": "transport",
            "runtime_stopped": True,
            "steps_completed": 0,
            "transport_summary": {"attempts": 1, "successes": 0, "failures": 1, "latency_ms": None},
            **changes,
        },
        True,
    )


def test_zero_successful_tunnels_allow_bounded_replacement_without_erasing_action_marker(db, proxy_runtime):
    work, _ = register_and_claim(db)
    with db.begin() as connection:
        connection.get(SessionAttempt, work.lease).started_action = True
    session_runner.finish(work, failed_transport())
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        assert row.status == "queued"
        assert row.metadata_json["pre_action_transport_failure_verified"]
        assert connection.get(SessionAttempt, work.lease).started_action
        assert connection.scalar(select(ProxyLease)).status == "released"


@pytest.mark.parametrize(
    "metadata",
    [
        {"non_idempotent_action_started": True},
        {"steps_completed": 1},
        {"transport_summary": {"attempts": 2, "successes": 1, "failures": 1}},
        {"transport_summary": {}},
        {"failure_kind": "authentication"},
        {"failure_kind": "quota"},
        {"failure_kind": "site_denial"},
        {"failure_kind": "budget_exhausted"},
        {"runtime_stopped": False},
    ],
)
def test_transport_recovery_never_replays_ambiguous_actions_or_auth_quota_denial(
    db, proxy_runtime, metadata
):
    work, identity = register_and_claim(db)
    transport_events = (
        [{"reason": "proxy_tunnel_timeout", "status": None}]
        if metadata.get("failure_kind", "transport") == "transport"
        else []
    )
    session_runner.finish(work, failed_transport(transport_events=transport_events, **metadata))
    with db() as connection:
        assert connection.get(BrowserSession, work.id).status != "queued"
        if metadata.get("failure_kind") in {"authentication", "quota"}:
            assert not connection.get(ProxyInventory, identity).enabled


def test_expired_proxy_lease_is_recorded_separately_from_tunnel_failure(db, proxy_runtime):
    work, _ = register_and_claim(db)
    assert session_runner.heartbeat(work) is False
    with db.begin() as connection:
        connection.scalar(select(ProxyLease)).expires_at = platform_service.utc_after(-1)

    assert session_runner.heartbeat(work) is True
    session_runner.finish(work, BrowserResult("cancelled", 100, None, {"runtime_stopped": True}, False))

    with client() as api:
        response = api.get(f"/api/v1/sessions/{work.id}/events")
        assert response.status_code == 200, response.text
        timeline = {item["kind"]: item["detail"] for item in response.json()["items"]}
    assert timeline["proxy_lease_expired"] == {"attempt": 1}
    assert timeline["session_finished"]["outcome"] == "cancelled"
    assert "proxy_transport_issue" not in timeline
    assert "connection_recovery_queued" not in timeline


def permit_next_claim(db):
    from abrl.db import Target

    with db.begin() as connection:
        connection.get(Target, "target").last_started_at = None
        connection.execute(
            update(BrowserSession)
            .where(BrowserSession.status == "queued", BrowserSession.next_attempt_at.is_not(None))
            .values(next_attempt_at=platform_service.utc_after(-1))
        )
        session_runner.admission_tick(connection)
    return session_runner.claim("standard")


def test_residential_recovery_uses_new_binding_epoch_and_stops_at_retry_budget(db, proxy_runtime):
    work, _ = register_and_claim(db)
    session_runner.finish(work, failed_transport())
    recovered = permit_next_claim(db)
    assert recovered and recovered.id == work.id and recovered.epoch > work.epoch
    assert recovered.proxy["session_key"] != work.proxy["session_key"]
    assert recovered.proxy["strict_affinity"]
    session_runner.finish(recovered, failed_transport())
    with db() as connection:
        assert connection.get(BrowserSession, work.id).status == "failed"
        assert len(connection.scalars(select(SessionAttempt)).all()) == 2
        assert len(connection.scalars(select(BrowserSession)).all()) == 1


@pytest.mark.parametrize("started_action", [False, True])
def test_proxy_destination_denial_prevents_transport_recovery(db, proxy_runtime, started_action):
    work, _ = register_and_claim(db)
    binding = OxylabsBinding.from_config(work.proxy)
    denied = {
        "kind": "upstream_rejected",
        "status": 403,
        "reason": "proxy_destination_denied",
        "requested_authority": "assets.example:443",
        "upstream_authority": "93.184.216.34:443",
    }
    result = failed_transport(
        transport_summary={"attempts": 2, "successes": 0, "failures": 2},
        transport_events=[denied, {"kind": "transport", "status": None, "reason": "proxy_tunnel_timeout"}],
    )
    result.started_action = started_action
    session_runner.finish(work, result)

    assert permit_next_claim(db) is None
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        assert row.status == "failed" and row.epoch == work.epoch
        attempt = connection.scalars(select(SessionAttempt)).one()
        assert attempt.number == 1 and attempt.started_action is started_action
        lease = connection.scalars(select(ProxyLease)).one()
        assert attempt.proxy_lease_id == lease.id and lease.status == "released"
        assert lease.config["strict_affinity"] and lease.config["session_key"] == work.proxy["session_key"]
        assert OxylabsBinding.from_config(lease.config).authorization() == binding.authorization()
        issue = connection.scalar(select(SessionEvent).where(SessionEvent.kind == "proxy_transport_issue"))
        assert "upstream_authority" not in issue.detail
        assert "requested_authority" not in row.metadata_json["transport_events"][0]

    # Simulate an event persisted before the upgraded worker removed the addresses.
    with db.begin() as connection:
        issue = connection.scalar(select(SessionEvent).where(SessionEvent.kind == "proxy_transport_issue"))
        issue.detail = {**issue.detail, "upstream_authority": denied["upstream_authority"]}

    with client() as api:
        response = api.get(f"/api/v1/sessions/{work.id}/events")
        assert response.status_code == 200, response.text
        timeline = {item["kind"]: item["detail"] for item in response.json()["items"]}
    assert timeline["proxy_transport_issue"] == {
        "reason": "proxy_destination_denied",
        "attempt": 1,
        "upstream_status": 403,
    }
    assert "connection_recovery_queued" not in timeline


def test_proxy_transport_issue_survives_subsequent_attempt_in_timeline(db, proxy_runtime):
    work, _ = register_and_claim(db)
    session_runner.finish(
        work,
        failed_transport(
            transport_events=[{"reason": "proxy_tunnel_failed", "status": 502}],
            transport_summary={
                "attempts": 1, "successes": 0, "failures": 1, "latency_ms": None, "private": "raw",
            },
            lifecycle_timeline={"version": 1, "events": [
                {"event": "navigation_started", "elapsed_ms": 20},
                {"event": "failure_observed", "elapsed_ms": 100, "url": "https://private.example"},
            ]},
        ),
    )
    recovered = permit_next_claim(db)
    assert recovered is not None
    session_runner.finish(recovered, BrowserResult("completed", 100, None, {"runtime_stopped": True}, True))

    with client() as api:
        response = api.get(f"/api/v1/sessions/{work.id}/events")
        assert response.status_code == 200, response.text
        timeline = {item["kind"]: item["detail"] for item in response.json()["items"]}
    assert timeline["proxy_transport_issue"] == {
        "reason": "proxy_tunnel_failed",
        "attempt": 1,
        "upstream_status": 502,
    }
    failed_attempt = timeline["session_attempt_finished"]
    assert failed_attempt["attempt"] == 1 and failed_attempt["epoch"] == work.epoch
    assert failed_attempt["outcome"] == "failed"
    assert failed_attempt["failure"]["phase"] == "proxy"
    assert failed_attempt["failure"]["reason"] == "proxy_tunnel_failed"
    assert failed_attempt["failure"]["status"] == 502
    assert failed_attempt["transport_summary"] == {"attempts": 1, "successes": 0, "failures": 1}
    assert failed_attempt["lifecycle_timeline"]["events"][-1] == {
        "event": "failure_observed", "elapsed_ms": 100, "navigation_delta_ms": 80,
    }
    recovery = timeline["connection_recovery_queued"]
    assert recovery["attempt"] == 1 and recovery["previous_epoch"] == work.epoch
    assert recovery["next_attempt_at"]
    assert timeline["session_finished"] == {"attempt": 2, "outcome": "completed", "epoch": recovered.epoch}
    assert "proxy_lease_expired" not in timeline


def test_proxy_configuration_failure_cannot_queue_connection_recovery(db, proxy_runtime):
    work, _ = register_and_claim(db)
    session_runner.finish(work, failed_transport(
        transport_events=[{"kind": "transport", "reason": "proxy_gateway_tls_failed", "retryable": False}],
    ))

    assert permit_next_claim(db) is None
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        lease = connection.get(ProxyLease, row.proxy_lease_id)
        assert row.status == "failed" and row.next_attempt_at is None
        assert lease.status == "released"


def test_measured_traffic_survives_recovery_and_uses_each_immutable_proxy_rate(db, proxy_runtime):
    work, _ = register_and_claim(db)
    with db.begin() as connection:
        lease = connection.scalar(select(ProxyLease))
        lease.config = {**lease.config, "cost_per_gb": 5, "cost_currency": "USD"}
    first_usage = {
        "total_bytes": 150,
        "upstream_bytes": 100,
        "downstream_bytes": 50,
        "measurement": "browser_payload_transport_bytes",
    }
    session_runner.finish(work, failed_transport(transport_usage=first_usage))
    recovered = permit_next_claim(db)
    assert recovered
    with db.begin() as connection:
        row = connection.get(BrowserSession, work.id)
        lease = connection.get(ProxyLease, row.proxy_lease_id)
        lease.config = {**lease.config, "cost_per_gb": 7, "cost_currency": "USD"}
    second_usage = {
        "total_bytes": 250,
        "upstream_bytes": 200,
        "downstream_bytes": 50,
        "measurement": "browser_payload_transport_bytes",
    }
    session_runner.finish(recovered, failed_transport(transport_usage=second_usage))
    with db() as connection:
        records = connection.get(BrowserSession, work.id).metadata_json["usage_attempts"]
        assert [record["attempt_id"] for record in records] == [work.lease, recovered.lease]
        assert [record["proxy_bytes"] for record in records] == [150, 250]
        assert [record["cost_per_gb"] for record in records] == [5, 7]
        assert sum(record["estimated_proxy_cost"] for record in records) == pytest.approx(2.5e-6)


def test_fixed_recovery_excludes_old_binding_and_duplicate_registration(db, proxy_runtime):
    work, first = register_and_claim(db, product="dedicated_isp", continuity="fixed")
    with db.begin() as connection:
        original = connection.get(ProxyInventory, first)
        for identity, port in (("same-fixed-port", 8001), ("replacement-fixed-port", 8002)):
            connection.add(
                ProxyInventory(
                    id=identity,
                    project_id="default",
                    name=identity,
                    config={**original.config, "port": port},
                    enabled=True,
                )
            )
    session_runner.finish(work, failed_transport())
    recovered = permit_next_claim(db)
    assert recovered and recovered.proxy["port"] == 8002
    with db() as connection:
        lease = connection.scalar(
            select(ProxyLease).where(ProxyLease.session_id == work.id, ProxyLease.status == "active")
        )
        reasons = {rejected["id"]: rejected["reasons"] for rejected in lease.rationale["rejected"]}
        assert "fixed_binding_failed_before_action" in reasons[first]
        assert "fixed_binding_failed_before_action" in reasons["same-fixed-port"]


@pytest.mark.parametrize("limit", [1, 2])
def test_experiment_admission_concurrency_applies_across_batches(db, limit):
    from abrl.platform_models import BrowserProfile, SessionBatch

    with db.begin() as connection:
        first, second = [
            platform_service.create_batch(
                connection,
                BatchInput(
                    target_id="target",
                    workflow_id="workflow",
                    count=2,
                    concurrency=2,
                    idempotency_key="cross-group-" + suffix,
                ),
                "default",
            )
            for suffix in ("first", "second")
        ]
    with db.begin() as connection:
        for identity in (first.id, second.id):
            batch = connection.get(SessionBatch, identity)
            batch.config = {
                **batch.config,
                "experiment_run_id": "same-experiment-run",
                "experiment_concurrency": limit,
            }
        rows = connection.scalars(
            select(BrowserSession).order_by(BrowserSession.ordinal, BrowserSession.batch_id)
        ).all()
        for index, row in enumerate(rows):
            row.created_at = "2026-01-01T00:00:00+00:00"
            row.dispatch_order = index + 1
        session_runner.admission_tick(connection)
    with db() as connection:
        admitted = connection.scalars(select(BrowserSession).where(BrowserSession.admitted.is_(True))).all()
        assert len(admitted) == limit
        assert sorted(row.dispatch_order for row in admitted) == list(range(1, limit + 1))
    with db.begin() as connection:
        row = connection.get(BrowserSession, admitted[0].id)
        row.status, row.admitted = "failed", False
        profile = connection.get(BrowserProfile, row.profile_id)
        profile.owner_session_id, profile.status = row.id, "quarantined"
        session_runner.admission_tick(connection)
    with db() as connection:
        # An uncertain, failed owner still occupies a run slot until fenced recovery.
        assert (
            len(connection.scalars(select(BrowserSession).where(BrowserSession.admitted.is_(True))).all())
            == limit - 1
        )

@pytest.mark.parametrize("reason", ["country_mismatch", "asn_not_allowed", "hosting_provider", "authentication"])
async def test_decodo_preflight_block_releases_lease_before_launch(db, proxy_runtime, monkeypatch, tmp_path, reason):
    work, inventory_id = register_and_claim(db, provider="decodo", continuity="sticky")
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path)
    launches = []

    async def blocked(binding, origin, policy, cancelled):
        assert origin == SCOPED_ORIGIN and binding.provider == "decodo" and not await cancelled()
        return {"status": "blocked", "reason": reason, "blocked": True, "checks": {}, "timings_ms": {"total": 3}}

    monkeypatch.setattr(session_runner, "run_decodo_preflight", blocked)
    monkeypatch.setattr(session_runner, "execute_workflow", lambda **kwargs: launches.append(kwargs))
    await session_runner.execute(work)
    assert not launches
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        attempt = connection.get(SessionAttempt, work.lease)
        inventory = connection.get(ProxyInventory, inventory_id)
        assert row.status == "blocked" and row.workflow_outcome == "not_started"
        assert not attempt.started_action and row.next_attempt_at is None
        assert connection.get(ProxyLease, attempt.proxy_lease_id).status == "released"
        assert inventory.active_leases == 0
        assert inventory.enabled == (reason != "authentication")
        event = connection.scalars(select(SessionEvent).where(SessionEvent.kind == "proxy_preflight")).one()
        assert event.detail["attempt"] == attempt.number and event.detail["epoch"] == work.epoch
        assert row.metadata_json["step_failure"]["reason"] == reason
        timeline = row.metadata_json["lifecycle_timeline"]
        assert timeline["version"] == 1
        [milestone] = timeline["events"]
        assert milestone == {
            "event": "preflight_completed", "elapsed_ms": milestone["elapsed_ms"], "navigation_delta_ms": None,
        }
        assert type(milestone["elapsed_ms"]) is int and milestone["elapsed_ms"] >= 0


@pytest.mark.parametrize("value", [[0], [True], [-1], [2**32], ["1234"]])
def test_policy_asns_require_valid_numbers(value):
    with pytest.raises(ValueError):
        ProxyPolicyInput(name="Decodo", provider="decodo", allowed_asns=value)


def test_policy_asn_revisions(db):
    with client() as api:
        base = "/api/v1/proxy-policies"
        first = api.post(base, json={"name": "US", "provider": "decodo", "allowed_asns": [12345]}).json()
        assert first["allowed_asns"] == [12345]
        second = api.put(f"{base}/{first['id']}", json={"name": "US", "provider": "decodo"}).json()
        assert second["allowed_asns"] == [] and second["version"] == first["version"] + 1
        assert api.post(base, json={"name": "Other", "provider": "oxylabs", "allowed_asns": [12345]}).status_code == 422


def test_project_preflight_summary_tracks_missing_inputs_and_latency(db):
    from abrl.platform_service import event

    with db.begin() as connection:
        for elapsed in (10, 40, 30):
            event(connection, "default", "proxy_preflight", blocked=elapsed == 30, reason="country_mismatch",
                  checks={"sticky": {"status": "failed", "reason": "sticky_ip_changed"},
                          "country": {"status": "failed", "observed": "DE"}},
                  timings_ms={"total": elapsed, "lookup": [{"first_byte": elapsed - 1}]})
    with client() as api:
        summary = api.get("/api/v1/proxy-preflight/summary?limit=2").json()
    assert summary["attempts"] == 2 and summary["blocking_reasons"] == {"country_mismatch": 1}
    assert summary["missing_evidence_rates"]["country"] == 0
    assert summary["missing_evidence_rates"]["asn"] == 1
    assert summary["sticky_compared"] == summary["sticky_mismatches"] == 2
    assert summary["latency_ms"]["lookup.first_byte"] == {"samples": 2, "p50": 34, "p95": 39}


async def test_cancelled_decodo_probe_releases_before_launch(db, proxy_runtime, monkeypatch, tmp_path):
    work, inventory_id = register_and_claim(db, provider="decodo", continuity="sticky")
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path)

    async def cancelled(*_):
        return {"status": "cancelled", "reason": "cancelled", "blocked": False, "checks": {}, "timings_ms": {}}

    monkeypatch.setattr(session_runner, "run_decodo_preflight", cancelled)
    monkeypatch.setattr(session_runner, "execute_workflow", lambda **_: pytest.fail("Browser launched after stop"))
    await session_runner.execute(work)
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        assert row.status == "cancelled" and row.workflow_outcome == "not_started"
        assert not connection.get(SessionAttempt, work.lease).started_action
        assert connection.get(ProxyInventory, inventory_id).active_leases == 0


def test_confirmed_gateway_failure_explains_browser_navigation_error():
    result = BrowserResult("failed", 25, "Navigation failed", {
        "causal_failure": {"reason": "navigation_failed", "phase": "workflow", "step": 1},
        "transport_summary": {"attempts": 1, "successes": 0},
        "transport_events": [{"kind": "authentication", "status": 407}],
    }, True)
    assert session_runner._failure_detail(result, None) == {
        "phase": "proxy", "reason": "authentication", "step": 1, "status": 407, "elapsed_ms": 25,
    }
