import copy
import csv
import io
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from abrl.db import Base
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    Experiment,
    ExperimentRevision,
    ExperimentRun,
    Project,
    ProviderObservation,
    SessionAttempt,
    SessionBatch,
    SessionBatchItem,
)
from abrl.research import (
    _hash,
    _interval,
    comparison,
    experiment_comparison,
    export_comparison,
    holm_adjust,
)


@pytest.fixture
def database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Project(id="project", name="Owned research"))
        db.flush()
        yield db
    engine.dispose()


def batch(db, *, identifier="batch", **config):
    row = SessionBatch(
        id=identifier,
        project_id="project",
        name="Reference research",
        idempotency_key="reference-" + identifier,
        request_hash="reference",
        count=0,
        concurrency=1,
        status="completed",
        config={
            "target": {"name": "Owned test", "origin": "https://owned.example", "mode": "browser_only"},
            "workflow": {"name": "Submit fixture", "steps": [{"action": "navigate", "url": "/"}]},
            "target_id": "target",
            "target_version": 1,
            "workflow_id": "workflow",
            "workflow_version": 3,
            "mode": "live",
            "phase": "confirmation",
            "seed": 13,
            "reliability_target": 0.95,
            "preparation": {"mode": "none"},
            **config,
        },
    )
    db.add(row)
    db.flush()
    return row


def add_session(
    db,
    experiment,
    *,
    adapter="playwright",
    outcome="completed",
    status=None,
    verified=None,
    raw_code=0,
    observation_source="controlled_site",
    profile_id=None,
    recovered=False,
    metadata=None,
    mode="live",
    cost=None,
):
    experiment.count += 1
    identifier = f"{experiment.id}-session-{experiment.count:04}"
    profile_id = profile_id or f"{experiment.id}-profile-{experiment.count:04}"
    if not db.get(BrowserProfile, profile_id):
        db.add(
            BrowserProfile(
                id=profile_id,
                project_id="project",
                name=profile_id,
                adapter=adapter,
                persistence="persistent",
            )
        )
        db.flush()
    runtime = {
        "browser_version": "Chrome/149.1",
        "os": "Linux",
        "architecture": "x86_64",
        "wrapper_version": "1.62",
        "preparation_mode": "none",
    }
    if metadata is not None:
        runtime = metadata
    if cost is not None:
        runtime["cost_total"] = cost
        runtime["cost_currency"] = "EUR"
    row = BrowserSession(
        id=identifier,
        batch_id=experiment.id,
        project_id="project",
        profile_id=profile_id,
        adapter=adapter,
        persistence="persistent",
        mode=mode,
        pool="standard",
        ordinal=experiment.count,
        status=status or outcome,
        outcome=outcome,
        elapsed_ms=150,
        metadata_json=runtime,
        ground_truth="automated",
        epoch=2 if recovered else 1,
        attempt_number=2 if recovered else 1,
    )
    db.add(row)
    db.flush()
    for number in range(1, 3 if recovered else 2):
        attempt = SessionAttempt(
            id=f"{identifier}-a{number}",
            session_id=identifier,
            number=number,
            epoch=number,
            outcome="failed" if recovered and number == 1 else outcome,
            started_action=not recovered or number != 1,
            finished_at="2026-11-12T12:00:00+00:00",
        )
        db.add(attempt)
    db.flush()
    if verified is not None:
        observation = ProviderObservation(
            id=f"{identifier}-observation",
            project_id="project",
            session_id=identifier,
            attempt_id=f"{identifier}-a{2 if recovered else 1}",
            idempotency_key=identifier,
            verification_status=verified,
            verdict="Clean" if raw_code == 0 else "Unknown",
            raw_code=raw_code,
            accepted=raw_code == 0 and verified == "verified",
            source=observation_source,
            provider_version="5",
        )
        db.add(observation)
    db.flush()
    return row


def test_wilson_reference_and_holm_multiple_comparisons():
    rows = [{"cluster_id": str(i), "success": i < 50} for i in range(100)]
    interval = _interval(50, 100, rows, seed=13)
    assert interval["low"] == pytest.approx(0.4038315303659956)
    assert interval["high"] == pytest.approx(0.5961684696340044)
    assert interval["method"] == "wilson"
    assert holm_adjust([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def test_cluster_bootstrap_does_not_turn_repeated_profile_into_independent_evidence():
    rows = [{"cluster_id": str(i % 2), "success": True} for i in range(300)]
    interval = _interval(300, 300, rows, seed=99)
    assert interval["method"] == "cluster_bootstrap_cluster_wilson_envelope"
    assert interval["clusters"] == 2
    assert interval["low"] < 0.5
    assert not interval["eligible"]
    assert interval == _interval(300, 300, rows, seed=99)
    single = _interval(300, 300, [{"cluster_id": "one", "success": True}] * 300, seed=99)
    assert single["low"] is None and not single["eligible"]


def test_instrumented_combined_success_verification_coverage_and_unknown_codes(database):
    experiment = batch(database, target={"mode": "instrumented"})
    add_session(database, experiment, verified="verified", raw_code=0)
    add_session(database, experiment, verified="verified", raw_code=9)
    add_session(database, experiment, verified="failed", raw_code=0)
    add_session(database, experiment, verified="verified", raw_code=987)
    add_session(database, experiment, outcome="failed", verified="verified", raw_code=0)
    report = comparison(database, experiment)
    group = report["groups"][0]
    assert group["n"] == 5
    assert group["completed"] == 4
    assert group["successes"] == 1
    assert group["verified"] == 4
    assert group["verification_coverage"] == 0.8
    assert group["verified_clean_rate"] == 0.5
    assert group["verdicts"] == {"0": 2, "9": 1, "987": 1}
    assert report["trials"][0]["observations"][0]["verdict"] == "Clean"
    assert report["trials"][2]["verification_status"] == "failed"


def test_conflicting_verified_codes_cannot_be_cherry_picked(database):
    experiment = batch(database, target={"mode": "instrumented"})
    row = add_session(database, experiment, verified="verified", raw_code=0)
    database.add(
        ProviderObservation(
            id="conflict",
            project_id="project",
            session_id=row.id,
            attempt_id=f"{row.id}-a1",
            idempotency_key="second-verdict",
            verification_status="verified",
            verdict="Bot",
            raw_code=9,
            source="controlled_site",
            provider_version="5",
        )
    )
    database.flush()
    report = comparison(database, experiment)
    assert report["groups"][0]["successes"] == 0
    assert report["trials"][0]["conflicting"]


def test_failed_prelaunch_sessions_stay_in_same_adapter_denominator(database):
    experiment = batch(database)
    for _ in range(79):
        add_session(database, experiment)
    add_session(database, experiment, outcome="failed", metadata={})
    report = comparison(database, experiment)
    assert len(report["groups"]) == 1
    assert report["groups"][0]["n"] == 80
    assert report["groups"][0]["successes"] == 79
    assert report["trials"][-1]["environment_attribution"] == "declared_group_prelaunch_failure"


def test_retries_do_not_expand_sample_and_first_attempt_and_cost_are_separate(database):
    experiment = batch(database)
    add_session(database, experiment, recovered=True, cost=4)
    add_session(database, experiment, outcome="failed", cost=2)
    report = comparison(database, experiment)
    group = report["groups"][0]
    assert group["n"] == 2 and group["successes"] == 1
    assert group["first_attempt"]["successes"] == 0
    assert group["recovered"] == 1
    assert group["cost_total"] == 6 and group["cost_per_success"] == 6
    assert len(report["trials"][0]["attempts"]) == 2


def test_unknown_cost_remains_unknown(database):
    experiment = batch(database)
    add_session(database, experiment, cost=4)
    add_session(database, experiment)
    group = comparison(database, experiment)["groups"][0]
    assert group["cost_total"] is None and group["cost_per_success"] is None


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"mode": "demo"}, "simulated"),
        ({"phase": "pilot"}, "pilot"),
    ],
)
def test_demo_and_pilot_never_final_qualify(database, override, expected):
    experiment = batch(database, **override)
    for _ in range(80):
        add_session(database, experiment, mode=override.get("mode", "live"))
    assert comparison(database, experiment)["recommendation"]["status"] == expected


def test_incomplete_batch_never_final_qualifies(database):
    experiment = batch(database)
    for _ in range(80):
        add_session(database, experiment)
    experiment.count += 1
    experiment.status = "running"
    report = comparison(database, experiment)
    assert report["recommendation"]["status"] == "incomplete"


def test_mixed_simulated_evidence_cannot_enter_live_ranking(database):
    experiment = batch(database)
    for _ in range(80):
        add_session(database, experiment)
    add_session(database, experiment, mode="demo")
    report = comparison(database, experiment)
    assert len(report["groups"]) == 2
    assert report["recommendation"]["status"] == "incompatible_evidence"
    assert not report["comparisons"]


def test_qualified_candidates_ties_and_matched_environments(database):
    experiment = batch(database)
    for adapter in ("playwright", "cloakbrowser"):
        for _ in range(80):
            add_session(database, experiment, adapter=adapter)
    report = comparison(database, experiment)
    assert report["recommendation"]["status"] == "tie"
    assert len(report["recommendation"]["candidates"]) == 2
    assert len(report["comparisons"]) == 1
    assert report["comparisons"][0]["adjusted_p_value"] == 1
    assert not report["comparisons"][0]["significant"]
    assert all(g["low"] >= 0.95 for g in report["groups"])


def test_different_os_not_claimed_as_matched_pair(database):
    experiment = batch(database)
    add_session(database, experiment)
    add_session(
        database,
        experiment,
        adapter="cloakbrowser",
        metadata={"browser_version": "Chrome/149", "os": "Darwin", "architecture": "arm64"},
    )
    report = comparison(database, experiment)
    assert len(report["groups"]) == 2
    assert report["comparisons"] == []


def test_export_dataset_trace_consistency_and_tamper_rejection(database, tmp_path):
    experiment = batch(database, mode="demo")
    add_session(database, experiment, mode="demo", recovered=True)
    add_session(database, experiment, mode="demo", outcome="failed")
    data = comparison(database, experiment)
    decoded = json.loads(export_comparison(data, "json"))
    assert decoded["snapshot"]["trials"] == decoded["trials"]
    rows = list(csv.DictReader(io.StringIO(export_comparison(data, "csv").decode())))
    assert len(rows) == 2
    assert rows[0]["snapshot_sha256"] == data["snapshot_sha256"]
    assert len(json.loads(rows[0]["attempts"])) == 2
    markup = export_comparison(data, "html").decode()
    assert "SIMULATED - NOT LIVE EVIDENCE" in markup
    assert data["trials"][0]["id"] in markup
    pdf = export_comparison(data, "pdf")
    assert pdf.startswith(b"%PDF")
    (tmp_path / "research.pdf").write_bytes(pdf)
    changed = copy.deepcopy(data)
    changed["groups"][0]["successes"] = 999
    with pytest.raises(ValueError, match="analysis checksum"):
        export_comparison(changed, "pdf")
    changed = copy.deepcopy(data)
    changed["snapshot"]["trials"][0]["success"] = False
    with pytest.raises(ValueError, match="snapshot checksum"):
        export_comparison(changed, "json")


def test_verified_callback_source_simulation_is_not_provider_evidence(database):
    experiment = batch(database, target={"mode": "instrumented"})
    add_session(database, experiment, verified="verified", raw_code=0, observation_source="simulation")
    group = comparison(database, experiment)["groups"][0]
    assert group["verified"] == 0 and group["successes"] == 0


def test_operational_failures_and_cancellations_can_yield_no_qualified_candidate(database):
    experiment = batch(database)
    for index in range(80):
        add_session(database, experiment, outcome="failed" if index % 2 else "cancelled")
    report = comparison(database, experiment)
    assert report["groups"][0]["n"] == 80
    assert report["recommendation"]["status"] == "no_qualified_candidate"


def test_diagnostic_rates_keep_ground_truth_and_denominators_separate():
    from abrl.research import diagnostic_rates

    rows = [
        {
            "verified": True,
            "raw_codes": [9],
            "ground_truth": "human",
            "observations": [],
            "complete": True,
            "workflow_completed": True,
            "status": "completed",
        },
        {
            "verified": True,
            "raw_codes": [0],
            "ground_truth": "automated",
            "observations": [],
            "complete": True,
            "workflow_completed": True,
            "status": "completed",
        },
        {
            "verified": False,
            "raw_codes": [],
            "ground_truth": "automated",
            "observations": [{"verification_status": "failed"}],
            "complete": True,
            "workflow_completed": False,
            "status": "failed",
        },
    ]
    result = diagnostic_rates(rows)
    assert result["bot_rate"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    assert result["false_positive_bot_rate"]["rate"] == 1
    assert result["automated_clean_rate"]["rate"] == 1
    assert result["verification_coverage"]["denominator"] == 3


def research_run(db, batches):
    config = {"name": "Cross-group research", "groups": [b.config for b in batches]}
    db.add(Experiment(id="experiment", project_id="project", name=config["name"], config=config))
    db.flush()
    db.add(ExperimentRevision(experiment_id="experiment", version=1, config=config))
    run = ExperimentRun(
        id="experiment-run",
        experiment_id="experiment",
        version=1,
        idempotency_key="run-request",
        batch_ids=[b.id for b in batches],
        randomization={"seed": 13, "algorithm": "seeded_cross_group_shuffle_v1", "order": []},
    )
    db.add(run)
    db.flush()
    return run


def test_run_aggregates_separate_adapter_batches_and_exports_all_trials(database):
    batches = [batch(database, identifier=name) for name in ("standard", "cloak")]
    for row, adapter in zip(batches, ("playwright", "cloakbrowser"), strict=True):
        for _ in range(80):
            add_session(database, row, adapter=adapter)
    report = experiment_comparison(database, research_run(database, batches))
    assert len(report["strata"]) == 1 and len(report["groups"]) == 2
    assert len(report["trials"]) == 160
    assert len(report["comparisons"]) == 1
    assert report["strata"][0]["recommendation"]["status"] == "tie"
    assert report["recommendation"]["candidates"] == []
    rows = list(csv.DictReader(io.StringIO(export_comparison(report, "csv").decode())))
    assert {r["batch_id"] for r in rows} == {"standard", "cloak"}
    assert len(rows) == 160 and all(r["run_id"] == "experiment-run" for r in rows)
    assert all(r["target_id"] == "target" and r["workflow_version"] == "3" for r in rows)
    assert "Matched conditions and decisions" in export_comparison(report, "html").decode()
    assert export_comparison(report, "pdf").startswith(b"%PDF")
    assert json.loads(export_comparison(report, "json"))["snapshot"]["planned"] == 160
    # Each nested report is independently traceable after run-wide Holm correction.
    for stratum in report["strata"]:
        assert (
            json.loads(export_comparison(stratum, "json"))["analysis_sha256"] == stratum["analysis_sha256"]
        )


def test_run_never_pools_workflows_environments_or_evidence_modes(database):
    one = batch(database, identifier="one")
    two = batch(database, identifier="two", workflow_id="different-workflow")
    three = batch(database, identifier="three")
    four = batch(database, identifier="four", target={"mode": "instrumented"})
    add_session(database, one)
    add_session(database, two, adapter="cloakbrowser")
    add_session(
        database,
        three,
        adapter="cloakbrowser",
        metadata={"browser_version": "Chrome/149", "os": "Darwin", "architecture": "arm64"},
    )
    add_session(database, four, verified="verified")
    report = experiment_comparison(database, research_run(database, [one, two, three, four]))
    assert len(report["strata"]) == 4 and not report["comparisons"]
    assert all(g["n"] == 1 for g in report["groups"])


def test_incomplete_run_blocks_otherwise_qualified_strata(database):
    finished = batch(database, identifier="finished")
    pending = batch(database, identifier="pending", workflow_id="other-workflow")
    for _ in range(80):
        add_session(database, finished)
    add_session(database, pending, outcome=None, status="queued")
    pending.status = "running"
    report = experiment_comparison(database, research_run(database, [finished, pending]))
    assert report["status"] == "incomplete"
    assert all(s["recommendation"]["status"] == "incomplete" for s in report["strata"])


def test_failed_creation_stays_in_operational_denominator(database):
    row = batch(database)
    add_session(database, row)
    row.count += 1
    row.status = "creation_failed"
    database.add(
        SessionBatchItem(
            batch_id=row.id,
            ordinal=2,
            adapter="playwright",
            status="failed",
            requested_profile_id=None,
            error="Profile creation unavailable",
            attempts=1,
        )
    )
    database.flush()
    report = comparison(database, row)
    assert len(report["trials"]) == 2
    assert report["groups"][0]["n"] == 2
    assert report["groups"][0]["successes"] == 1
    assert report["trials"][1]["outcome"] == "creation_failed"
    assert report["trials"][1]["session_id"] is None
    assert report["trials"][1]["record_source"] == "batch_creation"
    assert report["recommendation"]["status"] == "incomplete"


def test_run_holm_correction_covers_tests_in_every_stratum(database):
    batches = []
    for workflow in ("workflow-a", "workflow-b"):
        for adapter in ("playwright", "cloakbrowser"):
            row = batch(database, identifier=f"{workflow}-{adapter}", workflow_id=workflow)
            for i in range(20):
                add_session(
                    database,
                    row,
                    adapter=adapter,
                    outcome="failed" if adapter == "cloakbrowser" and i < 5 else "completed",
                )
            batches.append(row)
    report = experiment_comparison(database, research_run(database, batches))
    assert len(report["comparisons"]) == 2
    raw = [p["p_value"] for p in report["comparisons"]]
    assert [p["adjusted_p_value"] for p in report["comparisons"]] == pytest.approx(holm_adjust(raw))
    assert len({p["stratum_id"] for p in report["comparisons"]}) == 2


def test_run_keeps_preparation_policies_separate_and_rejects_mixed_simulation(database):
    one = batch(database, identifier="short", preparation={"mode": "automated", "max_seconds": 30})
    two = batch(database, identifier="long", preparation={"mode": "automated", "max_seconds": 90})
    for _ in range(80):
        add_session(database, one)
    add_session(database, two, mode="demo")
    report = experiment_comparison(database, research_run(database, [one, two]))
    assert len(report["strata"]) == 2 and report["comparisons"] == []
    assert report["recommendation"]["status"] == "incompatible_evidence"
    assert all(s["recommendation"]["candidates"] == [] for s in report["strata"])
    rows = list(csv.DictReader(io.StringIO(export_comparison(report, "csv").decode())))
    assert {r["mode"] for r in rows} == {"live", "demo"}


@pytest.mark.parametrize("key", ["request_budget", "traffic_budget_bytes"])
def test_different_execution_budgets_are_not_pooled_as_matched_conditions(database, key):
    one = batch(database, identifier="lower-budget", **{key: 1_000})
    two = batch(database, identifier="higher-budget", **{key: 2_000})
    add_session(database, one)
    add_session(database, two, adapter="cloakbrowser")
    report = experiment_comparison(database, research_run(database, [one, two]))
    assert len(report["strata"]) == 2 and not report["comparisons"]


def test_metered_proxy_component_is_traceable_but_not_a_complete_workflow_cost(database):
    from abrl.costs import record_attempt_usage

    row = batch(database)
    session = add_session(database, row, recovered=True)
    records = []
    for attempt, volume in ((1, 250_000_000), (2, 750_000_000)):
        records = record_attempt_usage(
            {"usage_attempts": records},
            {"transport_usage": {"measurement": "browser_payload_transport_bytes", "total_bytes": volume}},
            attempt_id=f"{session.id}-a{attempt}",
            proxy_config={"cost_per_gb": 2, "cost_currency": "EUR"},
        )
    session.metadata_json = {**session.metadata_json, "usage_attempts": records}
    database.flush()
    report = comparison(database, row)
    group = report["groups"][0]
    assert group["n"] == 1 and group["cost_total"] is None
    assert group["cost_metering"]["known_proxy_subtotals"] == {"EUR": 2}
    assert group["cost_metering"]["measured_proxy_bytes"] == 1_000_000_000
    assert group["cost_metering"]["measured_attempts"] == 2
    assert {r["attempt_id"] for r in report["trials"][0]["cost_details"]["usage_attempts"]} == {
        a["id"] for a in report["trials"][0]["attempts"]
    }


def test_historical_frozen_report_without_cost_metering_still_exports(database):
    row = batch(database, mode="demo")
    add_session(database, row, mode="demo")
    historical = comparison(database, row)
    for group in historical["groups"]:
        group.pop("cost_metering")
    for trial in historical["trials"]:
        trial.pop("cost_details")
    historical["snapshot_sha256"] = _hash(historical["snapshot"])
    historical["analysis_sha256"] = _hash({k: v for k, v in historical.items() if k != "analysis_sha256"})
    assert "historical snapshot" in export_comparison(historical, "html").decode()
    assert export_comparison(historical, "pdf").startswith(b"%PDF")
