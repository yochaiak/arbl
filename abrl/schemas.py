import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from abrl.network import normalize_origin


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
    authorized: Literal[True]

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
]

ASSERTIONS = {"assert_visible", "assert_text", "assert_not_visible"}


class Step(Input):
    action: Action
    url: str | None = Field(default=None, max_length=1500)
    selector: str | None = Field(default=None, min_length=1, max_length=512)
    value: str | None = Field(default=None, max_length=1000)
    secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
    name: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    timeout_ms: int = Field(default=5000, ge=100, le=15000)

    @model_validator(mode="after")
    def fields(self):
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
        if self.action == "navigate":
            if not self.url:
                raise ValueError("Navigate requires a site-relative path or an approved HTTPS URL")
            absolute = urlsplit(self.url)
            if absolute.scheme or absolute.netloc:
                if absolute.scheme != "https":
                    raise ValueError("Navigate requires a site-relative path or an approved HTTPS URL")
                normalize_origin(f"{absolute.scheme}://{absolute.netloc}")
            elif not self.url.startswith("/") or self.url.startswith("//"):
                raise ValueError("Navigate requires a site-relative path or an approved HTTPS URL")
            if (
                "\\" in self.url
                or re.search(r"[\x00-\x20]", self.url)
                or absolute.path.startswith("/.well-known/")
            ):
                raise ValueError("Invalid navigation path")
            if self.selector is not None:
                raise ValueError("Navigate does not use a selector")
        elif not self.selector or self.url is not None:
            raise ValueError("Provide a selector for this step")
        if self.action == "fill":
            if (self.value is None) == (self.secret_ref is None):
                raise ValueError("Fill requires exactly one value or secret reference")
        elif self.secret_ref is not None:
            raise ValueError("Only fill accepts a secret reference")
        elif self.action in ("select", "assert_text"):
            if self.value is None:
                raise ValueError("This action requires a value")
        elif self.value is not None:
            raise ValueError("This action does not use a value")
        return self


class WorkflowInput(Input):
    name: str = Field(min_length=1, max_length=100)
    steps: list[Step] = Field(min_length=2, max_length=40)

    @model_validator(mode="after")
    def required_steps(self):
        if self.steps[0].action != "navigate":
            raise ValueError("Begin the workflow with Navigate")
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
    seed: int = Field(default=42, ge=0, le=2147483647)
    phase: Literal["pilot", "confirmation"] = "pilot"
    mode: Literal["live", "demo"] = "demo"
    reliability_target: float = Field(default=0.95, gt=0, le=1)


class TargetView(TargetInput):
    id: str
    version: int
    verified: bool
    proof_path: str | None = None
    proof_token: str | None = None


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
