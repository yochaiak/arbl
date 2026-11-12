"""Workflow guard and checkpoint validation at the public API boundary."""

import pytest

from abrl.schemas import Step, TargetInput, WorkflowInput


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
