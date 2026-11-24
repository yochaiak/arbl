"""Workflow guard and checkpoint validation at the public API boundary."""

from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest

from abrl import browser
from abrl.network import FIXTURE_ORIGIN
from abrl.schemas import (
    Step,
    TargetInput,
    WorkflowBlockInput,
    WorkflowInput,
    resolve_navigation_parameters,
    resolve_workflow_inputs,
    validate_navigation_workflow_scope,
    validate_workflow_page_paths,
    walk_workflow_steps,
    workflow_parameter_names,
)


def workflow(*steps):
    return WorkflowInput(name="Guarded action", steps=[{"action": "navigate", "url": "/lab"}, *steps])


def test_absence_assertion_is_a_supported_explicit_success_condition():
    value = workflow({"action": "assert_not_visible", "selector": "#failure"})
    assert value.steps[-1].action == "assert_not_visible"


def test_named_checkpoint_roundtrips_only_after_successful_assertion():
    value = workflow(
        {"action": "assert_visible", "selector": "#ready"},
        {"action": "checkpoint", "name": "ready-to-submit"},
        {"action": "assert_not_visible", "selector": "#failure"},
    )
    assert WorkflowInput.model_validate(value.model_dump()).steps[2].name == "ready-to-submit"


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"name": "not a slug"},
        {"name": "ready", "selector": "#submit"},
        {"name": "ready", "url": "/lab"},
        {"name": "ready", "value": "unsafe"},
        {"name": "ready", "secret_ref": "sensitive"},
    ],
)
def test_checkpoint_does_not_accept_an_unnamed_or_action_payload(changes):
    with pytest.raises(ValueError):
        Step(action="checkpoint", **changes)


def test_checkpoint_cannot_turn_an_unconfirmed_action_into_a_safe_boundary():
    with pytest.raises(ValueError, match="immediately after"):
        workflow(
            {"action": "click", "selector": "#submit"},
            {"action": "checkpoint", "name": "sent"},
            {"action": "assert_visible", "selector": "#success"},
        )


def test_checkpoint_cannot_replace_the_final_assertion_or_duplicate_a_name():
    with pytest.raises(ValueError, match="Finish with"):
        workflow(
            {"action": "assert_visible", "selector": "#ready"}, {"action": "checkpoint", "name": "ready"}
        )
    with pytest.raises(ValueError, match="unique"):
        workflow(
            {"action": "assert_visible", "selector": "#ready"},
            {"action": "checkpoint", "name": "ready"},
            {"action": "assert_text", "selector": "#ready", "value": "Ready"},
            {"action": "checkpoint", "name": "ready"},
            {"action": "assert_not_visible", "selector": "#failure"},
        )


def test_only_checkpoint_can_have_a_checkpoint_name():
    with pytest.raises(ValueError, match="Only checkpoint"):
        Step(action="assert_visible", selector="#ready", name="ready")


@pytest.mark.parametrize("wait_ms", [None, 0, 750, 15_000, 15_001, 60_000])
def test_post_navigation_wait_roundtrips(wait_ms):
    data = workflow()
    assert data.steps[0].post_navigation_wait_ms is None
    payload = data.model_dump()
    payload["steps"][0]["post_navigation_wait_ms"] = wait_ms
    saved = WorkflowInput.model_validate(payload).model_dump()
    assert saved["steps"][0]["post_navigation_wait_ms"] == wait_ms


@pytest.mark.parametrize("schema_version", [1, 2])
def test_initial_navigation_timeout_inherits_only_when_omitted(schema_version):
    config = WorkflowInput(
        name="Navigation defaults",
        schema_version=schema_version,
        steps=[
            {"action": "navigate", "url": "/lab"},
            {"action": "navigate", "url": "/lab/form", "timeout_ms": 5000},
            {"action": "navigate", "url": "/lab/form", "timeout_ms": 20_000},
            {"action": "assert_visible", "selector": "#ready"},
        ],
    ).model_dump(mode="json")
    assert "initial_navigation_timeout_ms" not in config
    assert "timeout_ms" not in config["steps"][0]
    assert [step["timeout_ms"] for step in config["steps"][1:]] == [5000, 20_000, 5000]

    config["initial_navigation_timeout_ms"] = 18_000
    assert WorkflowInput.model_validate(config).model_dump(mode="json") == config


@pytest.mark.parametrize("timeout_ms", [99, 120_001, 8000.5, True, "18000"])
def test_workflow_navigation_timeout_must_be_a_bounded_integer(timeout_ms):
    with pytest.raises(ValueError, match="initial_navigation_timeout_ms"):
        WorkflowInput(
            name="Invalid timeout", steps=[{"action": "navigate", "url": "/lab"}],
            initial_navigation_timeout_ms=timeout_ms,
        )


def test_version_one_other_step_timeouts_retain_their_limit():
    with pytest.raises(ValueError, match="non-navigation step timeouts"):
        workflow({"action": "assert_visible", "selector": "#ready", "timeout_ms": 20_000})


@pytest.mark.parametrize("wait_ms", [-1, 60_001, 1.5, True])
def test_post_navigation_wait_must_be_a_bounded_integer(wait_ms):
    with pytest.raises(ValueError, match="post_navigation_wait_ms"):
        Step(action="navigate", url="/lab", post_navigation_wait_ms=wait_ms)


def test_only_navigate_accepts_a_post_navigation_wait():
    with pytest.raises(ValueError, match="Only Navigate"):
        Step(action="assert_visible", selector="#ready", post_navigation_wait_ms=500)


def test_navigation_origin_permissions_are_normalized_and_separate_from_resources():
    target = TargetInput(
        name="Owned application",
        origin="https://app.example",
        authorized=True,
        navigation_origins=[
            "https://app.example",
            "https://CHECKOUT.example:443/",
            "https://checkout.example",
        ],
        resource_origins=["https://checkout.example", "https://assets.example"],
    )
    assert target.navigation_origins == ["https://checkout.example"]
    assert target.resource_origins == ["https://assets.example"]
    assert Step(action="navigate", url="https://checkout.example/confirm").url.endswith("/confirm")


@pytest.mark.parametrize(
    "url",
    [
        "http://checkout.example",
        "https://user@checkout.example/path",
        "//checkout.example/path",
        "https://127.0.0.1/path",
        "https://checkout.example/.well-known/proof",
    ],
)
def test_absolute_navigation_rejects_untrusted_url_forms(url):
    with pytest.raises(ValueError):
        Step(action="navigate", url=url)


@pytest.mark.parametrize("url", ["/qa/check", f"{FIXTURE_ORIGIN}/lab?mode=check", "https://app.example/qa"])
def test_one_navigation_is_enough_to_save_a_workflow(url):
    value = WorkflowInput(name="Site navigation", steps=[{"action": "navigate", "url": url}])
    assert len(value.steps) == 1
    assert value.steps[0].url == url


@pytest.mark.parametrize(
    "step",
    [
        Step(action="navigate", url="https://app.example/qa/check"),
        Step(
            action="navigate",
            url="https://app.example/qa/check?q=[QUERY]",
            fixture_params={"QUERY": "sample"},
        ),
    ],
)
def test_single_external_navigation_uses_registered_scope_for_a_local_target(step):
    validate_navigation_workflow_scope(
        [step.model_dump()],
        origin=FIXTURE_ORIGIN,
        navigation_origins=["https://app.example"],
        resource_origins=["https://assets.example"],
    )


@pytest.mark.parametrize("url", ["/qa#section", "https://example.com/qa#"])
def test_one_step_rejects_fragments(url):
    with pytest.raises(ValueError, match="without a fragment"):
        WorkflowInput(name="Unsupported one-step workflow", steps=[{"action": "navigate", "url": url}])


def test_one_step_fixture_workflow_retains_and_resolves_its_absolute_template():
    url = f"{FIXTURE_ORIGIN}/lab?term=[QUERY]&pub_sub_id=[SUBID]"
    parameters = {"QUERY": "local & test", "SUBID": "run-1"}
    step = WorkflowInput(
        name="Local full URL",
        steps=[{"action": "navigate", "url": url, "fixture_params": parameters}],
    ).steps[0]
    assert step.url == url
    assert resolve_navigation_parameters(step.url, step.fixture_params) == (
        f"{FIXTURE_ORIGIN}/lab?term=local+%26+test&pub_sub_id=run-1"
    )


@pytest.mark.parametrize("origin", ["", "https://app.example"])
def test_url_parameters_encode_values_and_preserve_duplicate_keys(origin):
    step = Step(
        action="navigate",
        url=f"{origin}/qa/verify?term=%5BQUERY%5D&tag=&tag=[SUBID]",
        fixture_params={"QUERY": "  alpha & next=β #  ", "SUBID": "qa=2"},
    )
    assert step.fixture_params == {"QUERY": "alpha & next=β #", "SUBID": "qa=2"}
    resolved = resolve_navigation_parameters(step.url, step.fixture_params)
    assert resolved.startswith(f"{origin}/qa/verify?")
    assert parse_qsl(urlsplit(resolved).query, keep_blank_values=True) == [
        ("term", "alpha & next=β #"),
        ("tag", ""),
        ("tag", "qa=2"),
    ]


def test_named_parameters_preserve_literal_query_text_and_match_case():
    url = (
        "/qa?static=%2f%2B&flag&&%74ag=[CAMPAIGN_ID]&tag=%5Bcampaign_id%5D&tag=[CAMPAIGN_ID]&spaces=a%20b&"
    )
    step = Step(
        action="navigate",
        url=url,
        fixture_params={"campaign_id": "a+b%", "CAMPAIGN_ID": "été #!"},
    )
    assert workflow_parameter_names([step.model_dump()]) == ["CAMPAIGN_ID", "campaign_id"]
    assert resolve_navigation_parameters(step.url, step.fixture_params) == (
        "/qa?static=%2f%2B&flag&&%74ag=%C3%A9t%C3%A9+%23%21&tag=a%2Bb%25"
        "&tag=%C3%A9t%C3%A9+%23%21&spaces=a%20b&"
    )


def test_named_parameters_are_limited_across_workflow_steps():
    steps = [
        {"action": "navigate", "url": f"/qa?q=[P{index}]", "fixture_params": {f"P{index}": "value"}}
        for index in range(16)
    ]
    assertion = {"action": "assert_visible", "selector": "#ready"}
    repeated = {"action": "navigate", "url": "/qa?q=[P0]", "fixture_params": {"P0": "different default"}}
    accepted = WorkflowInput(name="Named parameters", steps=[*steps, repeated, assertion])
    assert workflow_parameter_names([step.model_dump() for step in accepted.steps]) == [
        f"P{index}" for index in range(16)
    ]
    with pytest.raises(ValueError, match="16 distinct URL parameters"):
        WorkflowInput(
            name="Too many parameters",
            steps=[
                *steps,
                {"action": "navigate", "url": "/qa?q=[NEW]", "fixture_params": {"NEW": "value"}},
                assertion,
            ],
        )


@pytest.mark.parametrize(
    ("url", "params"),
    [
        ("//app.example/qa?q=[QUERY]", {"QUERY": "sample"}),
        ("https://user@app.example/qa?q=[QUERY]", {"QUERY": "sample"}),
        ("https:///qa?q=[QUERY]", {"QUERY": "sample"}),
        ("/lab?q=[QUERY]#", {"QUERY": "sample"}),
        ("/lab?[QUERY]=placeholder&q=[QUERY]", {"QUERY": "sample"}),
        ("/lab?q=prefix-[QUERY]", {"QUERY": "sample"}),
        ("/qa/%5BCAMPAIGN_ID%5D?q=[QUERY]", {"QUERY": "sample"}),
        (f"/qa?q=[{'N' * 33}]", {"N" * 33: "sample"}),
        ("/lab?q=[QUERY]&extra=[UNKNOWN]", {"QUERY": "sample"}),
        ("/lab?q=[QUERY]&extra=%00", {"QUERY": "sample"}),
        ("/lab?q=[QUERY]", {"SUBID": "sample"}),
        ("/lab?q=[QUERY]", {"QUERY": "sample", "SUBID": "extra"}),
        ("/lab?q=[QUERY]", {"QUERY": "sample", "OTHER": "extra"}),
        ("/lab?q=[QUERY]", {"QUERY": "sample\n"}),
        ("/lab?q=[QUERY]", {"QUERY": "  "}),
        ("/lab?q=[QUERY]", {"QUERY": "😀" * 200}),
        ("/lab?q=[QUERY]", {}),
    ],
)
def test_fixture_url_parameters_reject_unsafe_or_mismatched_templates(url, params):
    with pytest.raises(ValueError):
        Step(action="navigate", url=url, fixture_params=params)


def test_existing_bracketed_urls_are_literal_when_no_fixture_parameters_are_set():
    step = Step(action="navigate", url="https://app.example/[CAMPAIGN_ID]?q=[QUERY]")
    assert step.fixture_params is None
    assert step.url == "https://app.example/[CAMPAIGN_ID]?q=[QUERY]"
    assert not workflow_parameter_names([step.model_dump()])


async def _never_cancel():
    return False


@pytest.mark.parametrize(
    "options",
    [
        {"navigation_origins": ["https://app.example"]},
        {"resource_origins": ["https://app.example"]},
        {"remote": {}},
    ],
)
async def test_parameterized_workflow_cannot_launch_outside_local_fixture(monkeypatch, options):
    async def unexpected_drive(*args, **kwargs):
        pytest.fail("The browser must not launch for an out-of-scope fixture template")

    monkeypatch.setattr(browser, "_drive", unexpected_drive)
    result = await browser.execute_workflow(
        **{"origin": FIXTURE_ORIGIN, "resource_origins": [], **options},
        steps=[{"action": "navigate", "url": "/lab?term=[QUERY]", "fixture_params": {"QUERY": "qa"}}],
        cancel=_never_cancel,
    )
    assert result.outcome == "failed"
    assert "local ABRL test site" in result.error
    assert not result.started_action


async def test_external_verification_navigation_resolves_values_before_adding_context(monkeypatch):
    origin = "https://app.example"
    seen = []

    async def wait_for_observation():
        return True

    async def capture_drive(steps, execution, **kwargs):
        seen.extend(steps)
        assert execution.wait_for_observation is wait_for_observation

    monkeypatch.setattr(browser, "_drive", capture_drive)
    url = f"{origin}/qa/verify?term=[QUERY]&tag=[SUBID]"
    steps = [{"action": "navigate", "url": url, "fixture_params": {"QUERY": "a & b", "SUBID": "qa#1"}}]
    result = await browser.execute_workflow(
        origin=origin,
        resource_origins=["https://assets.example"],
        steps=steps,
        cancel=_never_cancel,
        remote={},
        observation_context="signed token",
        wait_for_observation=wait_for_observation,
    )
    assert result.outcome == "completed"
    assert len(seen) == 1
    parts = urlsplit(seen[0]["url"])
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "app.example", "/qa/verify")
    assert parse_qs(parts.query) == {"term": ["a & b"], "tag": ["qa#1"], "abrl_context": ["signed token"]}
    assert steps[0]["url"] == url


async def test_parameterized_navigation_rejects_a_resource_only_origin(monkeypatch):
    async def unexpected_drive(*args, **kwargs):
        pytest.fail("The browser must not launch for an out-of-scope navigation template")

    monkeypatch.setattr(browser, "_drive", unexpected_drive)
    result = await browser.execute_workflow(
        origin="https://app.example",
        resource_origins=["https://assets.example"],
        steps=[
            {
                "action": "navigate",
                "url": "https://assets.example/qa?q=[QUERY]",
                "fixture_params": {"QUERY": "qa"},
            }
        ],
        cancel=_never_cancel,
    )
    assert result.outcome == "failed"
    assert "registered target scope" in result.error
    assert not result.started_action


async def test_browser_prepares_resolved_url_without_mutating_workflow_snapshot(monkeypatch):
    seen = []

    async def capture_drive(steps, *args, **kwargs):
        seen.extend(steps)

    monkeypatch.setattr(browser, "_drive", capture_drive)
    steps = [{"action": "navigate", "url": "/lab?term=[QUERY]", "fixture_params": {"QUERY": "a&b #"}}]
    result = await browser.execute_workflow(
        origin=FIXTURE_ORIGIN,
        resource_origins=[],
        steps=steps,
        cancel=_never_cancel,
    )
    assert result.outcome == "completed"
    assert seen[0]["url"] == "/lab?term=a%26b+%23"
    assert steps[0]["url"] == "/lab?term=[QUERY]"


def test_v2_workflow_and_frozen_block_round_trip():
    block = WorkflowBlockInput.model_validate({
        "name": "Fill fields",
        "steps": [{"action": "fill", "selector": "#name", "value_ref": "title"}],
    })
    config = WorkflowInput.model_validate({
        "schema_version": 2,
        "name": "Form",
        "inputs": {"title": {"type": "string", "default": "  Preserved  "}},
        "steps": [
            {"action": "checkpoint", "name": "before_navigation"},
            {"action": "navigate", "url": "/lab"},
            {"action": "use_block", "block_id": "form", "block_version": 2,
             "steps": [step.model_dump() for step in block.steps]},
        ],
    }).model_dump(mode="json")
    assert WorkflowInput.model_validate(config).model_dump(mode="json") == config
    assert validate_workflow_page_paths(config["steps"])
    assert [step["action"] for step in walk_workflow_steps(config["steps"])] == [
        "checkpoint", "navigate", "use_block", "fill",
    ]
    assert resolve_workflow_inputs(config) == {"title": "  Preserved  "}


def test_navigation_timeout_omission_survives_nested_blocks():
    block = WorkflowBlockInput(name="Open two pages", steps=[
        {
            "action": "repeat", "max_iterations": 2, "steps": [
                {"action": "open_tab", "tab": "new", "url": "/lab"},
                {"action": "navigate", "url": "/lab", "timeout_ms": 5000},
            ],
        },
    ]).model_dump(mode="json")
    children = block["steps"][0]["steps"]
    assert "timeout_ms" not in children[0]
    assert children[1]["timeout_ms"] == 5000
    assert WorkflowBlockInput.model_validate(block).model_dump(mode="json") == block


def test_v2_requires_all_paths_to_initialize_a_document():
    payload = {
        "schema_version": 2,
        "name": "Conditional entry",
        "inputs": {"open": {"type": "boolean"}},
        "steps": [
            {
                "action": "if",
                "condition": {"kind": "value_equals", "value_ref": "open", "value": True},
                "steps": [{"action": "navigate", "url": "/lab"}],
            },
            {"action": "fill", "selector": "#name", "value": "Hello"},
        ],
    }
    with pytest.raises(ValueError, match="Open a page before running a browser action"):
        WorkflowInput.model_validate(payload)
    payload["steps"][0]["else_steps"] = [{"action": "open_tab", "tab": "fallback", "url": "/lab"}]
    assert WorkflowInput.model_validate(payload).schema_version == 2


def test_v2_inputs_are_typed_and_unknown_names_are_rejected():
    config = {
        "inputs": {
            "quantity": {"type": "number", "default": 2},
            "active": {"type": "boolean"},
            "optional": {"type": "string", "required": False},
        },
    }
    assert resolve_workflow_inputs(config, {"active": False, "quantity": 1.5}) == {
        "quantity": 1.5, "active": False,
    }
    with pytest.raises(ValueError, match="quantity must use type number"):
        resolve_workflow_inputs(config, {"active": False, "quantity": True})
    with pytest.raises(ValueError, match="Provide a value for workflow input active"):
        resolve_workflow_inputs(config)
    with pytest.raises(ValueError, match="undeclared"):
        resolve_workflow_inputs(config, {"other": 1})


def test_v2_nesting_and_execution_values_do_not_change_v1_validation(monkeypatch):
    steps = [{"action": "navigate", "url": "/lab"}] + [
        {"action": "assert_visible", "selector": "#main"} for _ in range(45)
    ]
    with pytest.raises(ValueError, match="up to 40 steps"):
        WorkflowInput(name="Longer legacy workflow", steps=steps)
    assert len(WorkflowInput(schema_version=2, name="Longer workflow", steps=steps).steps) == 46
    monkeypatch.setenv("ABRL_MAX_WORKFLOW_STEPS", "45")
    with pytest.raises(ValueError, match="configured step limit"):
        WorkflowInput(schema_version=2, name="Too many steps", steps=steps)


def test_v2_recursive_scope_rejects_unused_unapproved_tab():
    config = WorkflowInput.model_validate({
        "schema_version": 2,
        "name": "Tab scope",
        "steps": [
            {"action": "navigate", "url": "/qa"},
            {"action": "if", "condition": {"kind": "hidden", "selector": "#optional"},
             "steps": [{"action": "open_tab", "url": "https://outside.example/qa", "tab": "outside"}]},
        ],
    }).model_dump(mode="json")
    with pytest.raises(ValueError, match="outside the registered target scope"):
        validate_navigation_workflow_scope(
            config["steps"], origin="https://example.com", navigation_origins=[], resource_origins=[],
            all_navigation=True,
        )
