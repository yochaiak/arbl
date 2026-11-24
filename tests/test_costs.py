import json

import pytest
import test_platform
from test_platform import client

from abrl.costs import fleet_cost, record_attempt_usage, session_cost
from abrl.db import now
from abrl.platform_models import FleetSlot
from abrl.platform_service import utc_after

db = test_platform.db


def usage(total, vendor=0):
    return {
        "transport_usage": {
            "measurement": "browser_payload_transport_bytes",
            "total_bytes": total,
            "upstream_bytes": total // 4,
            "downstream_bytes": total - total // 4,
            "vendor_control_bytes": vendor,
        },
        "vendor_control_bytes": vendor,
    }


def test_proxy_cost_accumulates_recovery_and_excludes_vendor_control_traffic():
    first = record_attempt_usage(
        {},
        usage(1_000_000_000, 9_000_000_000),
        attempt_id="first",
        proxy_config={"cost_per_gb": 2, "cost_currency": "EUR"},
    )
    second = record_attempt_usage(
        {"usage_attempts": first},
        usage(500_000_000),
        attempt_id="second",
        proxy_config={"cost_per_gb": 4, "cost_currency": "EUR"},
    )
    # Redelivery replaces that attempt's cumulative counter instead of double counting.
    repeated = record_attempt_usage(
        {"usage_attempts": second},
        usage(500_000_000),
        attempt_id="second",
        proxy_config={"cost_per_gb": 4, "cost_currency": "EUR"},
    )
    result = session_cost({"usage_attempts": repeated}, {})
    assert len(repeated) == 2
    assert result["estimated_proxy_cost"] == 4
    assert result["measured_proxy_bytes"] == 1_500_000_000
    assert result["total"] is None
    assert result["missing_components"] == ["infrastructure", "browser_license"]
    assert result["billing_status"] == "not_reconciled_to_invoice"
    # A proxy tariff's unit does not supply a missing unit for an unrelated all-in amount.
    assert session_cost({"usage_attempts": repeated, "cost_total": 10}, {})["total"] is None


@pytest.mark.parametrize("proxy", [{"cost_per_gb": 2}, {"cost_currency": "EUR"}, {}])
def test_rate_or_currency_missing_never_invents_a_proxy_price(proxy):
    rows = record_attempt_usage({}, usage(1_000_000_000), attempt_id="one", proxy_config=proxy)
    assert session_cost({"usage_attempts": rows}, {})["estimated_proxy_cost"] is None


def test_proxy_usage_attributes_provider_from_leased_inventory():
    (record,) = record_attempt_usage(
        {}, usage(1_000), attempt_id="one", proxy_config={"provider": "decodo"}
    )
    assert record["provider"] == "decodo"
    assert record["proxy_bytes"] == 1_000


def test_direct_connection_zero_proxy_component_does_not_imply_free_workflow():
    rows = record_attempt_usage({}, usage(1_000), attempt_id="one", proxy_config=None)
    result = session_cost({"usage_attempts": rows}, {})
    assert result["estimated_proxy_cost"] == 0 and result["total"] is None
    result = session_cost(
        {
            "usage_attempts": rows,
            "infrastructure_cost_total": 2,
            "browser_cost_total": 0,
            "cost_currency": "EUR",
        },
        {},
    )
    assert result["total"] == 2 and result["basis"] == "component_estimate"
    assert session_cost({"cost_total": 5}, {})["total"] is None


def test_mixed_currency_attempts_keep_separate_subtotals():
    rows = record_attempt_usage(
        {}, usage(1_000_000_000), attempt_id="one", proxy_config={"cost_per_gb": 2, "cost_currency": "EUR"}
    )
    rows = record_attempt_usage(
        {"usage_attempts": rows},
        usage(1_000_000_000),
        attempt_id="two",
        proxy_config={"cost_per_gb": 3, "cost_currency": "USD"},
    )
    result = session_cost({"usage_attempts": rows}, {})
    assert result["known_proxy_subtotals"] == {"EUR": 2, "USD": 3}
    assert result["estimated_proxy_cost"] is None and result["total"] is None


def test_missing_crashed_attempt_usage_preserves_subtotal_but_blocks_complete_cost():
    rows = record_attempt_usage(
        {},
        usage(1_000_000_000),
        attempt_id="recovery",
        proxy_config={"cost_per_gb": 2, "cost_currency": "EUR"},
    )
    result = session_cost({"usage_attempts": rows}, {}, attempt_ids=["crashed", "recovery"])
    assert result["known_proxy_subtotals"] == {"EUR": 2}
    assert result["estimated_proxy_cost"] is None and result["total"] is None
    assert result["missing_usage_attempts"] == ["crashed"]
    assert result["missing_usage_attempt_count"] == 1
    explicit = session_cost(
        {"usage_attempts": rows},
        {"cost_per_session": 10, "cost_currency": "EUR"},
        attempt_ids=["crashed", "recovery"],
    )
    assert explicit["total"] == 10 and explicit["basis"] == "configured_all_in_estimate"


def test_fleet_deduplicates_observed_nodes_and_discloses_inventory_gaps():
    workers = [
        {"id": "one", "ready": True, "node_name": "node-1", "pool": "standard"},
        {"id": "two", "ready": True, "node_name": "node-1", "pool": "standard"},
        {"id": "old", "ready": False, "node_name": "node-old", "pool": "vendor"},
    ]
    result = fleet_cost(workers, {"currency": "EUR", "node_hourly": {"standard": 0.75}})
    assert result["hourly_estimate"] == 0.75 and len(result["observed_nodes"]) == 1
    assert result["total_fleet_hourly_estimate"] is None
    assert "active_runner_nodes_only" in result["inventory_coverage"]
    workers.append({"id": "vendor", "ready": True, "node_name": "node-2", "pool": "vendor"})
    result = fleet_cost(workers, {"currency": "EUR", "node_hourly": {"standard": 0.75}})
    assert result["hourly_estimate"] is None and result["known_hourly_subtotal"] == 0.75
    assert "node_price:node-2" in result["missing_prices_or_identity"]


def test_fleet_api_uses_configured_prices_and_actual_worker_identity(db, monkeypatch):
    monkeypatch.setenv(
        "ABRL_FLEET_PRICES_JSON", json.dumps({"currency": "EUR", "node_hourly": {"standard": 1.2}})
    )
    with db.begin() as connection:
        connection.add(
            FleetSlot(
                id="worker-one",
                pool="standard",
                capacity=1,
                seen_at=now(),
                metadata_json={"node_name": "observed-node"},
            )
        )
    with client() as api:
        response = api.get("/api/v1/fleet")
    assert response.status_code == 200
    assert response.json()["cost"]["hourly_estimate"] == 1.2
    assert response.json()["cost"]["billing_status"] == "estimated_not_billed"


def test_worker_inventory_filters_paginates_and_validates_order(db):
    with db.begin() as connection:
        connection.add_all(
            [
                FleetSlot(
                    id="worker-a",
                    pool="standard",
                    capacity=1,
                    seen_at=now(),
                    metadata_json={"node_name": "node-percent%"},
                ),
                FleetSlot(
                    id="worker-b",
                    pool="vendor",
                    capacity=1,
                    seen_at=utc_after(-120),
                    metadata_json={"node_name": "node-old"},
                ),
                FleetSlot(
                    id="worker-c",
                    pool="standard",
                    capacity=1,
                    seen_at=now(),
                    metadata_json={"node_name": "node-percent-other"},
                ),
            ]
        )
    with client() as api:
        page = api.get(
            "/api/v1/fleet/workers?pool=standard&ready=true&order=name_desc&limit=1&offset=1"
        ).json()
        assert page["total"] == 2 and page["items"][0]["id"] == "worker-a"
        assert api.get("/api/v1/fleet/workers?q=%25").json()["total"] == 1
        assert api.get("/api/v1/fleet/workers?ready=false").json()["items"][0]["id"] == "worker-b"
        assert api.get("/api/v1/fleet/workers?order=untrusted").status_code == 422
