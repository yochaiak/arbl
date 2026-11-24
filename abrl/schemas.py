import os
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import Annotated, Literal
from urllib.parse import SplitResult, quote_plus, unquote, unquote_plus, urljoin, urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from abrl.network import FIXTURE_ORIGIN, is_fixture_path, normalize_origin, request_origin


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TargetInput(Input):
    name: str = Field(min_length=1, max_length=100)
    origin: str = Field(max_length=256)
    navigation_origins: list[str] = Field(default_factory=list, max_length=20)
    resource_origins: list[str] = Field(default_factory=list, max_length=20)
    mode: Literal["browser_only", "instrumented"] = "browser_only"
    concurrency: int = Field(default=1, ge=1, le=500)
    adscore_zone_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    adscore_zone_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$")
    acceptance_gate: Literal["verified_clean", "observe_only"] = "verified_clean"
    rate_limit: int = Field(default=30, ge=1, le=6000)
    session_policy: Literal["fresh", "repeat_within_trial"] = "fresh"
    authorized: Literal[True] = True

    @model_validator(mode="after")
    def origins(self):
        self.origin = normalize_origin(self.origin)
        self.navigation_origins = sorted(
            {normalize_origin(value) for value in self.navigation_origins} - {self.origin}
        )
        if any(urlsplit(value).scheme != "https" for value in self.navigation_origins):
            raise ValueError("Additional navigation origins must use HTTPS")
        if self.mode == "instrumented":
            self.resource_origins = list({*self.resource_origins, "https://c.adsco.re"})
        self.resource_origins = sorted(
            {normalize_origin(value) for value in self.resource_origins}
            - {self.origin, *self.navigation_origins}
        )
        if any(urlsplit(value).scheme != "https" for value in self.resource_origins):
            raise ValueError("Additional resource origins must use HTTPS")
        return self


Action = Literal[
    "navigate",
    "click",
    "fill",
    "select",
    "extract",
    "wait_for",
    "assert_visible",
    "assert_text",
    "assert_not_visible",
    "checkpoint",
    "check",
    "uncheck",
    "press",
    "hover",
    "wait_for_text",
    "wait_for_hidden",
    "wait_for_url",
    "assert_url",
    "if",
    "repeat",
    "use_block",
    "open_tab",
    "switch_tab",
    "close_tab",
    "expect_popup",
]

ASSERTIONS = {"assert_visible", "assert_text", "assert_not_visible", "assert_url"}
LEGACY_ACTIONS = {
    "navigate", "click", "fill", "select", "extract", "wait_for",
    "assert_visible", "assert_text", "assert_not_visible", "checkpoint",
}
CONTROL_ACTIONS = {"if", "repeat", "use_block"}
NAVIGATION_ACTIONS = {"navigate", "open_tab"}
MAX_NAVIGATION_TIMEOUT_MS = 120_000
TAB_ACTIONS = {"open_tab", "switch_tab", "close_tab", "expect_popup"}
OBSERVATION_ACTIONS = ASSERTIONS | {
    "extract", "wait_for", "wait_for_hidden", "wait_for_text", "wait_for_url", "checkpoint",
}
_V2_STEP_FIELDS = {
    "save_as", "value_ref", "condition", "steps", "else_steps", "max_iterations",
    "block_id", "block_version", "tab", "frame",
}

_PARAMETER_NAME = r"[A-Za-z][A-Za-z0-9_]{0,31}"
_FIXTURE_PLACEHOLDER = re.compile(r"\[([A-Za-z][A-Za-z0-9_]*)\]")
_FIXTURE_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
FixtureParameterName = Annotated[str, Field(pattern=rf"^{_PARAMETER_NAME}$")]
_MAX_WORKFLOW_PARAMETERS = 16
_MAX_NAVIGATION_URL = 1500
_NAVIGATION_URL_REQUIRED = (
    "Navigate requires a site-relative path, local fixture URL, or approved HTTPS URL"
)


def _trim_fixture_parameter(value: object) -> object:
    if isinstance(value, str):
        if _FIXTURE_CONTROLS.search(value):
            raise ValueError("URL parameters must not contain control characters")
        return value.strip()
    return value


FixtureParameter = Annotated[
    str, BeforeValidator(_trim_fixture_parameter), Field(min_length=1, max_length=200)
]
FixtureParams = Annotated[
    dict[FixtureParameterName, FixtureParameter],
    Field(min_length=1, max_length=_MAX_WORKFLOW_PARAMETERS),
]
_fixture_params_adapter = TypeAdapter(FixtureParams)


def _navigation_url_parts(url: str) -> SplitResult:
    if len(url) > _MAX_NAVIGATION_URL or "\\" in url or re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("Invalid navigation URL")
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        if not parts.scheme or not parts.netloc:
            raise ValueError(_NAVIGATION_URL_REQUIRED)
        request_origin(url)
    elif not url.startswith("/") or url.startswith("//"):
        raise ValueError(_NAVIGATION_URL_REQUIRED)
    if parts.path.startswith("/.well-known/"):
        raise ValueError("Invalid navigation path")
    return parts


def _navigation_parameter_parts(url: str) -> tuple[str, list[tuple[str, str, str | None]]]:
    parts = _navigation_url_parts(url)
    if "#" in url or _FIXTURE_CONTROLS.search(url):
        raise ValueError("URL parameters require a valid navigation URL without a fragment")
    if _FIXTURE_PLACEHOLDER.search(unquote(parts.path, errors="strict")):
        raise ValueError("Use URL placeholders as whole query values")
    query_parts = []
    for raw in parts.query.split("&"):
        raw_key, _, raw_value = raw.partition("=")
        key = unquote_plus(raw_key, errors="strict")
        value = unquote_plus(raw_value, errors="strict")
        if _FIXTURE_CONTROLS.search(key) or _FIXTURE_CONTROLS.search(value):
            raise ValueError("Query strings must not contain control characters")
        if _FIXTURE_PLACEHOLDER.search(key):
            raise ValueError("Use URL placeholders as whole query values")
        name = None
        if _FIXTURE_PLACEHOLDER.search(value):
            match = _FIXTURE_PLACEHOLDER.fullmatch(value)
            if not match or not re.fullmatch(_PARAMETER_NAME, match[1]):
                raise ValueError("Use URL placeholders of up to 32 characters as whole query values")
            name = match[1]
        query_parts.append((raw, raw_key, name))
    return url.partition("?")[0], query_parts


def walk_workflow_steps(steps: Sequence[Mapping]) -> Iterator[Mapping]:
    """Visit each stored step, including branches and frozen block revisions."""
    for step in steps:
        yield step
        for key in ("steps", "else_steps"):
            if children := step.get(key):
                yield from walk_workflow_steps(children)


def workflow_parameter_names(steps: Sequence[Mapping[str, object]]) -> list[FixtureParameterName]:
    names: dict[str, None] = {}
    for step in walk_workflow_steps(steps):
        url = step.get("url")
        if (
            step.get("action") in NAVIGATION_ACTIONS
            and isinstance(url, str)
            and step.get("fixture_params") is not None
        ):
            _, parts = _navigation_parameter_parts(url)
            names.update((name, None) for _, _, name in parts if name is not None)
    return list(names)


def resolve_navigation_parameters(url: str, params: FixtureParams) -> str:
    base, parts = _navigation_parameter_parts(url)
    values = _fixture_params_adapter.validate_python(params)
    used = {name for _, _, name in parts if name is not None}
    if used - values.keys():
        raise ValueError("Provide a value for every URL placeholder")
    if values.keys() - used:
        raise ValueError("Provide values only for URL placeholders in the template")
    query = "&".join(
        raw if name is None else f"{key}={quote_plus(values[name])}" for raw, key, name in parts
    )
    resolved = f"{base}?{query}"
    if len(resolved) > _MAX_NAVIGATION_URL:
        raise ValueError("Resolved navigation URL exceeds 1500 characters")
    return resolved


def validate_navigation_workflow_scope(
    steps: Sequence[Mapping[str, object]],
    *,
    origin: str,
    resource_origins: Collection[str],
    navigation_origins: Collection[str],
    fixture_origin: str = FIXTURE_ORIGIN,
    remote: bool = False,
    constrain_navigation_only: bool = True,
    all_navigation: bool = False,
) -> None:
    for step in walk_workflow_steps(steps):
        if step.get("action") not in NAVIGATION_ACTIONS:
            continue
        parameterized = step.get("fixture_params") is not None
        if not all_navigation and not parameterized and (not constrain_navigation_only or len(steps) != 1):
            continue
        url = step.get("url")
        if not isinstance(url, str) or ("#" in url and (not all_navigation or parameterized)):
            raise ValueError("A single Navigate step or URL template requires a URL without a fragment")
        if parameterized:
            url = resolve_navigation_parameters(url, step["fixture_params"])
        else:
            _navigation_url_parts(url)
        absolute = urljoin(origin + "/", url)
        destination = request_origin(absolute)
        if destination not in {origin, *navigation_origins} or (
            destination == fixture_origin and origin != fixture_origin
        ):
            raise ValueError("Navigation outside the registered target scope")
        uses_fixture_scope = origin == fixture_origin and (destination == fixture_origin or remote)
        if uses_fixture_scope and (
            remote
            or any(allowed != origin for allowed in (*resource_origins, *navigation_origins))
            or not is_fixture_path(absolute)
            or (parameterized and urlsplit(absolute).path != "/lab")
        ):
            raise ValueError(
                "The local ABRL test site requires a local runner and fixture route with no external origins"
            )


def _optional_value(value) -> bool:
    return value is None


WorkflowValue = (
    Annotated[str, StringConstraints(strip_whitespace=False, max_length=4000, strict=True)]
    | Annotated[float, Field(strict=True, allow_inf_nan=False)]
    | Annotated[int, Field(strict=True)]
    | Annotated[bool, Field(strict=True)]
)


class WorkflowInputDefinition(Input):
    type: Literal["string", "number", "boolean"]
    default: WorkflowValue | None = Field(default=None, exclude_if=_optional_value)
    required: bool = True

    def accepts(self, value: object) -> bool:
        if self.type == "string":
            return type(value) is str
        if self.type == "number":
            return type(value) in (int, float)
        return type(value) is bool

    @model_validator(mode="after")
    def validate_default(self):
        if self.default is not None and not self.accepts(self.default):
            raise ValueError("Input default must match its declared type")
        return self


def resolve_workflow_inputs(workflow: Mapping, supplied: Mapping | None = None) -> dict:
    supplied = supplied or {}
    definitions = workflow.get("inputs") or {}
    if set(supplied) - definitions.keys():
        raise ValueError("Workflow inputs contain an undeclared name")
    resolved = {}
    for name, saved in definitions.items():
        definition = WorkflowInputDefinition.model_validate(saved)
        value = supplied.get(name, definition.default)
        if value is None:
            if definition.required or name in supplied:
                raise ValueError(f"Provide a value for workflow input {name}")
            continue
        if not definition.accepts(value):
            raise ValueError(f"Workflow input {name} must use type {definition.type}")
        resolved[name] = _workflow_value_adapter.validate_python(value)
    return resolved


_workflow_value_adapter = TypeAdapter(WorkflowValue)


class WorkflowCondition(Input):
    kind: Literal["visible", "hidden", "text_contains", "url_contains", "value_equals"]
    selector: str | None = Field(default=None, min_length=1, max_length=512, exclude_if=_optional_value)
    text: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=1000)] | None = Field(
        default=None, exclude_if=_optional_value
    )
    value_ref: FixtureParameterName | None = Field(default=None, exclude_if=_optional_value)
    value: WorkflowValue | None = Field(default=None, exclude_if=_optional_value)

    @model_validator(mode="after")
    def condition_fields(self):
        expected = {
            "visible": {"selector"},
            "hidden": {"selector"},
            "text_contains": {"selector", "text"},
            "url_contains": {"text"},
            "value_equals": {"value_ref", "value"},
        }[self.kind]
        actual = {name for name in ("selector", "text", "value_ref", "value") if getattr(self, name) is not None}
        if expected != actual:
            raise ValueError(f"{self.kind} condition requires {', '.join(sorted(expected))}")
        return self


class Step(Input):
    action: Action
    url: str | None = Field(default=None, max_length=_MAX_NAVIGATION_URL)
    fixture_params: FixtureParams | None = None
    selector: str | None = Field(default=None, min_length=1, max_length=512)
    value: Annotated[str, StringConstraints(strip_whitespace=False, max_length=1000)] | None = None
    secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
    name: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    timeout_ms: int | None = Field(
        default=None, ge=100, le=MAX_NAVIGATION_TIMEOUT_MS, exclude_if=_optional_value,
        description="Override the initial navigation timeout for Navigate or Open tab. Other steps default to 5000 ms.",
    )
    post_navigation_wait_ms: int | None = Field(
        default=None,
        ge=0,
        le=60_000,
        strict=True,
        description="Milliseconds to wait after navigation before completing the step.",
    )
    save_as: FixtureParameterName | None = Field(default=None, exclude_if=_optional_value)
    value_ref: FixtureParameterName | None = Field(default=None, exclude_if=_optional_value)
    condition: WorkflowCondition | None = Field(default=None, exclude_if=_optional_value)
    steps: list["Step"] | None = Field(default=None, exclude_if=_optional_value)
    else_steps: list["Step"] | None = Field(default=None, exclude_if=_optional_value)
    max_iterations: int | None = Field(default=None, ge=1, le=100, exclude_if=_optional_value)
    block_id: str | None = Field(default=None, min_length=1, max_length=36, exclude_if=_optional_value)
    block_version: int | None = Field(default=None, ge=1, exclude_if=_optional_value)
    tab: FixtureParameterName | None = Field(default=None, exclude_if=_optional_value)
    frame: list[Annotated[str, Field(min_length=1, max_length=512)]] | None = Field(
        default=None, min_length=1, max_length=12, exclude_if=_optional_value
    )

    @model_validator(mode="after")
    def fields(self):
        if self.timeout_ms is None and self.action not in NAVIGATION_ACTIONS:
            self.timeout_ms = 5000
        extra_fields = {
            "save_as": {"extract"},
            "value_ref": {"fill", "select", "press", "wait_for_text", "assert_text"},
            "condition": {"if", "repeat"},
            "steps": CONTROL_ACTIONS,
            "else_steps": {"if"},
            "max_iterations": {"repeat"},
            "block_id": {"use_block"},
            "block_version": {"use_block"},
            "tab": TAB_ACTIONS,
            "frame": {"click", "fill", "select", "extract", "wait_for", "assert_visible", "assert_text",
                      "assert_not_visible", "check", "uncheck", "press", "hover", "wait_for_text",
                      "wait_for_hidden", "expect_popup", "if", "repeat"},
        }
        for field, actions in extra_fields.items():
            if getattr(self, field) is not None and self.action not in actions:
                raise ValueError(f"{self.action} does not accept {field}")
        if self.fixture_params is not None and self.action not in NAVIGATION_ACTIONS:
            raise ValueError("Only Navigate accepts URL parameters")
        if self.post_navigation_wait_ms is not None and self.action != "navigate":
            raise ValueError("Only Navigate accepts a post-navigation wait")
        if self.action == "checkpoint":
            if not self.name:
                raise ValueError("Checkpoint requires a name")
            if any(value is not None for value in (self.url, self.selector, self.value, self.secret_ref)):
                raise ValueError(
                    "Checkpoint records an assertion boundary and does not accept page actions"
                )
            return self
        if self.name is not None:
            raise ValueError("Only checkpoint accepts a name")
        if self.action in TAB_ACTIONS and not self.tab:
            raise ValueError("Provide a tab name")
        if self.action == "close_tab" and self.tab == "main":
            raise ValueError("The main tab cannot be closed explicitly")
        if self.action in CONTROL_ACTIONS:
            if self.action in {"if", "repeat"} and not self.steps:
                raise ValueError(f"{self.action} requires nested steps")
            if self.action == "if" and not self.condition:
                raise ValueError("If requires a condition")
            if self.action == "repeat" and self.max_iterations is None:
                raise ValueError("Repeat requires a maximum iteration count")
            if self.action == "use_block" and (not self.block_id or self.block_version is None):
                raise ValueError("Use block requires a pinned revision")
        no_selector = CONTROL_ACTIONS | {"switch_tab", "close_tab", "wait_for_url", "assert_url"}
        if self.action in NAVIGATION_ACTIONS:
            if not self.url:
                raise ValueError(_NAVIGATION_URL_REQUIRED)
            if self.selector is not None:
                raise ValueError("Navigate does not use a selector")
            if self.fixture_params is not None:
                resolve_navigation_parameters(self.url, self.fixture_params)
            else:
                _navigation_url_parts(self.url)
        elif self.action in no_selector:
            if self.selector is not None or self.url is not None:
                raise ValueError(f"{self.action} does not use a selector or URL field")
        elif not self.selector or self.url is not None:
            raise ValueError("Provide a selector for this step")
        if self.action == "fill":
            if sum(value is not None for value in (self.value, self.secret_ref, self.value_ref)) != 1:
                raise ValueError("Fill requires exactly one value, input reference, or secret reference")
        elif self.secret_ref is not None:
            raise ValueError("Only fill accepts a secret reference")
        elif self.action in {"select", "assert_text", "press", "wait_for_text", "wait_for_url", "assert_url"}:
            if (self.value is None) == (self.value_ref is None):
                raise ValueError("This action requires one value or input reference")
            if self.action in {"press", "wait_for_url", "assert_url"} and self.value == "":
                raise ValueError("Provide a nonempty action value")
        elif self.value is not None:
            raise ValueError("This action does not use a value")
        return self


def _count_authored_steps(steps: Sequence[Step], depth: int = 1) -> int:
    if depth > 24:
        raise ValueError("Workflow nesting is too deep")
    count = len(steps)
    for step in steps:
        for body in (step.steps, step.else_steps):
            if body:
                count += _count_authored_steps(body, depth + 1)
    return count


def workflow_step_limit() -> int:
    return int(os.getenv("ABRL_MAX_WORKFLOW_STEPS", "500"))


def workflow_execution_limit() -> int:
    return int(os.getenv("ABRL_MAX_WORKFLOW_EXECUTED_STEPS", "5000"))


def validate_workflow_page_paths(
    steps: Sequence[Mapping], *, initial: bool = False, allow_unresolved: bool = False
) -> bool:
    """Require a document on every reachable path before document actions."""
    ready = initial
    for step in steps:
        action = step["action"]
        if action in NAVIGATION_ACTIONS:
            ready = True
        elif action == "if":
            condition = step["condition"]
            if condition["kind"] != "value_equals" and not ready:
                raise ValueError("Open a page before evaluating a page condition")
            on_match = validate_workflow_page_paths(
                step["steps"], initial=ready, allow_unresolved=allow_unresolved
            )
            on_miss = validate_workflow_page_paths(
                step.get("else_steps") or [], initial=ready, allow_unresolved=allow_unresolved
            )
            ready = on_match and on_miss
        elif action in {"repeat", "use_block"}:
            if not step.get("steps"):
                if not allow_unresolved:
                    raise ValueError("Workflow block revision has not been resolved")
                ready = True
            else:
                ready = validate_workflow_page_paths(
                    step["steps"], initial=ready, allow_unresolved=allow_unresolved
                )
            condition = step.get("condition")
            if condition and condition["kind"] != "value_equals" and not ready:
                raise ValueError("Open a page before evaluating a page condition")
        elif action != "checkpoint" and not ready:
            raise ValueError("Open a page before running a browser action")
    return ready


class WorkflowBlockInput(Input):
    name: str = Field(min_length=1, max_length=100)
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def bounded(self):
        if _count_authored_steps(self.steps) > workflow_step_limit():
            raise ValueError("Workflow block exceeds the configured step limit")
        return self


class WorkflowInput(Input):
    name: str = Field(min_length=1, max_length=100)
    steps: list[Step] = Field(min_length=1)
    initial_navigation_timeout_ms: int | None = Field(
        default=None, ge=100, le=MAX_NAVIGATION_TIMEOUT_MS, strict=True, exclude_if=_optional_value,
        description="Default initial timeout for navigation steps in this workflow. Explicit step values take precedence.",
    )
    schema_version: Literal[1, 2] = Field(default=1, exclude_if=lambda value: value == 1)
    inputs: dict[FixtureParameterName, WorkflowInputDefinition] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    success_check: WorkflowCondition | None = Field(default=None, exclude_if=_optional_value)

    @model_validator(mode="after")
    def required_steps(self):
        if self.schema_version == 2:
            if _count_authored_steps(self.steps) > workflow_step_limit():
                raise ValueError("Workflow exceeds the configured step limit")
            steps = [step.model_dump(mode="json") for step in self.steps]
            if any(step.get("save_as") in self.inputs for step in walk_workflow_steps(steps)):
                raise ValueError("A captured value cannot overwrite a declared input")
            if len(workflow_parameter_names(steps)) > _MAX_WORKFLOW_PARAMETERS:
                raise ValueError(f"Use at most {_MAX_WORKFLOW_PARAMETERS} distinct URL parameters per workflow")
            ready = validate_workflow_page_paths(steps, allow_unresolved=True)
            if self.success_check and self.success_check.kind != "value_equals" and not ready:
                raise ValueError("Open a page before running a final page check")
            return self
        if self.inputs or self.success_check or any(
            step.action not in LEGACY_ACTIONS or any(getattr(step, name) is not None for name in _V2_STEP_FIELDS)
            for step in self.steps
        ):
            raise ValueError("This workflow uses version 2 features")
        if len(self.steps) > 40:
            raise ValueError("Version 1 workflows support up to 40 steps")
        if any(step.action not in NAVIGATION_ACTIONS and (step.timeout_ms or 0) > 15000 for step in self.steps):
            raise ValueError("Version 1 non-navigation step timeouts cannot exceed 15000 milliseconds")
        if self.steps[0].action != "navigate":
            raise ValueError("Begin the workflow with Navigate")
        parameter_names = {name for step in self.steps for name in step.fixture_params or {}}
        if len(parameter_names) > _MAX_WORKFLOW_PARAMETERS:
            raise ValueError(f"Use at most {_MAX_WORKFLOW_PARAMETERS} distinct URL parameters per workflow")
        if len(self.steps) == 1:
            if "#" in (self.steps[0].url or ""):
                raise ValueError("A single Navigate step requires a URL without a fragment")
            return self
        if self.steps[-1].action not in ASSERTIONS:
            raise ValueError("Finish with an explicit success assertion")
        names = set()
        for index, step in enumerate(self.steps):
            if step.action != "checkpoint":
                continue
            if index == 0 or self.steps[index - 1].action not in ASSERTIONS:
                raise ValueError("Place each checkpoint immediately after an explicit assertion")
            if step.name in names:
                raise ValueError("Checkpoint names must be unique within a workflow")
            names.add(step.name)
        return self


class RunInput(Input):
    target_id: str = Field(min_length=1, max_length=36)
    workflow_id: str = Field(min_length=1, max_length=36)
    repetitions: int | None = Field(default=None, ge=1, le=300)
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    phase: Literal["pilot", "confirmation"] = "pilot"
    mode: Literal["live", "demo"] = "live"
    reliability_target: float = Field(default=0.95, gt=0, le=1)


class TargetView(TargetInput):
    id: str
    version: int
    verified: bool = Field(deprecated=True, description="Historical ownership verification status")
    live_ready: bool


class WorkflowView(WorkflowInput):
    id: str
    version: int


class RunView(BaseModel):
    id: str
    target_id: str
    workflow_id: str
    target_name: str
    workflow_name: str
    target_version: int
    workflow_version: int
    status: Literal["queued", "running", "paused", "cancelling", "cancelled", "completed", "blocked"]
    mode: Literal["live", "demo"]
    phase: Literal["pilot", "confirmation"]
    target_mode: Literal["browser_only", "instrumented"]
    repetitions: int
    seed: int
    created_at: str
    finished_at: str | None
    reliability_target: float


class TrialView(BaseModel):
    id: str
    ordinal: int
    status: str
    outcome: str | None
    elapsed_ms: int | None
    error: str | None
    attempts: int
    verification_status: Literal["not_collected"]
    verdict: None
    environment: dict


class IntervalView(BaseModel):
    successes: int
    total: int
    rate: float | None
    low: float | None
    high: float | None


class ReportView(RunView):
    run_id: str
    counts: dict[str, int]
    completion: IntervalView
    first_attempt: IntervalView
    latency_ms: dict[str, float | None]
    verdicts: dict[str, int]
    verification: dict[str, int]
    cost_per_success: float | None
    recommendation: dict[str, str]
    limitations: list[str]
    config_sha256: str
    snapshot: dict
    trials: list[TrialView]
