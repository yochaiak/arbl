"""Public proxy registration through durable admission and provider binding, without network calls."""

import base64

import pytest
from sqlalchemy import select
from test_platform import client, create
from test_platform import db as db

from abrl import platform_service, session_runner
from abrl.browser import BrowserResult
from abrl.db import TargetVersion
from abrl.platform_models import BrowserSession, ProxyInventory, ProxyLease, SessionAttempt
from abrl.platform_schemas import BatchInput, ProxyInventoryInput, ProxyPolicyInput
from abrl.proxy import OxylabsBinding, lease_proxy_config, select_proxy


@pytest.fixture
def proxy_runtime(monkeypatch):
    monkeypatch.setattr(
        platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
    )
    monkeypatch.setenv("ABRL_SECRET_PROXY_USER", "customer-authorized")
    monkeypatch.setenv("ABRL_SECRET_PROXY_PASSWORD", "private-provider-password")


def register_and_claim(db, *, continuity="strict", product="residential", duration=45, connection_load=1):
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": "https://authorized.example"}
    with client() as api:
        response = api.post(
            "/api/v1/proxy-inventory",
            json={
                "name": "Authorized test inventory",
                "product": product,
                "username_ref": "PROXY_USER",
                "password_ref": "PROXY_PASSWORD",
                "country": "IL",
                "capacity": 2,
                "max_duration_minutes": 60,
                "exclusive": product.startswith("dedicated_"),
            },
        )
        assert response.status_code == 201, response.text
        inventory_id = response.json()["id"]
        response = api.post(
            "/api/v1/proxy-policies",
            json={
                "name": "Transport continuity",
                "country": "IL",
                "products": [product],
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


def test_policy_connection_load_reserves_and_releases_actual_broker_capacity(db, proxy_runtime):
    work, inventory_id = register_and_claim(db, connection_load=2)
    assert work.proxy["max_connections"] == 2
    with db() as connection:
        assert connection.get(ProxyInventory, inventory_id).active_leases == 2
    session_runner.finish(work, failed_transport(failure_kind="budget_exhausted"))
    with db() as connection:
        assert connection.get(ProxyInventory, inventory_id).active_leases == 0


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
    session_runner.finish(work, failed_transport(**metadata))
    with db() as connection:
        assert connection.get(BrowserSession, work.id).status != "queued"
        if metadata.get("failure_kind") in {"authentication", "quota"}:
            assert not connection.get(ProxyInventory, identity).enabled


def permit_next_claim(db):
    from abrl.db import Target

    with db.begin() as connection:
        connection.get(Target, "target").last_started_at = None
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
