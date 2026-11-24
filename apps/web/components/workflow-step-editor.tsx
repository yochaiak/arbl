import { ArrowDown, ArrowUp, Copy, Plus, Trash2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  actions,
  actionLabels,
  api,
  conditionKinds,
  fixtureParameterMaxLength,
  legacyActions,
  navigationTimeoutMaxMs,
  postNavigationWaitMaxMs,
  workflowFixtureParameters,
  workflowInputTypes,
  workflowUrlPreview,
  type Step,
  type ValidationIssue,
  type WorkflowBlock,
  type WorkflowCondition,
  type WorkflowInputDefinition,
  type WorkflowScalar,
} from "@/lib/api";
import { platformKey } from "@/lib/platform";
import { WorkflowUrlPreview } from "./workflow-url-parameters";
import { ErrorNotice, Field, Select } from "./ui";

export type DraftStep = Omit<Step, "steps" | "else_steps"> & {
  key: number;
  steps?: DraftStep[] | null;
  else_steps?: DraftStep[] | null;
};

type Location = (string | number)[];

const valueActions = new Set(["fill", "select", "assert_text", "press", "wait_for_text", "wait_for_url", "assert_url"]);
const literalOnly = new Set(["wait_for_url", "assert_url"]);
const controlActions = new Set(["if", "repeat", "use_block"]);
const tabActions = new Set(["open_tab", "switch_tab", "close_tab", "expect_popup"]);
const navigationActions = new Set(["navigate", "open_tab"]);
const withoutSelector = new Set(["navigate", "open_tab", "switch_tab", "close_tab", "checkpoint", "wait_for_url", "assert_url"]);
const conditionLabels: Record<WorkflowCondition["kind"], string> = {
  visible: "Element is visible",
  hidden: "Element is hidden",
  text_contains: "Element contains text",
  url_contains: "Page URL contains text",
  value_equals: "Workflow value equals",
};

export function draftSteps(steps: Step[], allocate: () => number): DraftStep[] {
  return steps.map(({ steps, else_steps, ...step }) => ({
    ...step,
    key: allocate(),
    steps: steps ? draftSteps(steps, allocate) : steps,
    else_steps: else_steps ? draftSteps(else_steps, allocate) : else_steps,
  }));
}

export function serializeSteps(steps: DraftStep[]): Step[] {
  return steps.map(({ key, steps, else_steps, frame, ...step }) => {
    void key;
    const selectors = frame?.map((selector) => selector.trim()).filter(Boolean);
    return {
      ...step,
      ...(selectors?.length ? { frame: selectors } : {}),
      ...(steps ? { steps: serializeSteps(steps) } : {}),
      ...(else_steps?.length ? { else_steps: serializeSteps(else_steps) } : {}),
    };
  });
}

export function issuesAt(issues: ValidationIssue[], location: Location, local = false) {
  return issues.filter(({ path }) =>
    path.length >= location.length
    && (!local || path.length <= location.length + 1)
    && location.every((part, index) => path[index] === part)
  );
}

export function showIssues(issues: ValidationIssue[]) {
  return [...new Set(issues.map((issue) => issue.message))].join(" ");
}

export function ScalarField({
  label,
  type,
  value,
  onChange,
}: {
  label: string;
  type: WorkflowInputDefinition["type"];
  value: WorkflowScalar | null | undefined;
  onChange: (value: WorkflowScalar | undefined) => void;
}) {
  if (type === "boolean") {
    return <Field label={label}><Select value={value == null ? "" : String(value)} onChange={(event) => {
      onChange(event.target.value === "" ? undefined : event.target.value === "true");
    }}>
      <option value="">Choose a value</option>
      <option value="true">True</option>
      <option value="false">False</option>
    </Select></Field>;
  }
  return <Field label={label}><input
    type={type === "number" ? "number" : "text"}
    step={type === "number" ? "any" : undefined}
    value={value == null ? "" : String(value)}
    onChange={(event) => {
      const next = event.target.value;
      onChange(type === "number" ? next === "" ? undefined : event.target.valueAsNumber : next);
    }}
  /></Field>;
}

export function ConditionEditor({
  value,
  onChange,
}: {
  value: WorkflowCondition;
  onChange: (value: WorkflowCondition) => void;
}) {
  const { kind } = value;
  const [scalarType, setScalarType] = useState<WorkflowInputDefinition["type"]>(() => (
    typeof value.value === "number" ? "number" : typeof value.value === "boolean" ? "boolean" : "string"
  ));
  return <div className="form-grid">
    <Field label="Condition"><Select value={kind} onChange={(event) => {
      const next = event.target.value as WorkflowCondition["kind"];
      if (next === "value_equals") setScalarType("string");
      onChange(next === "value_equals" ? { kind: next, value: "" } : { kind: next });
    }}>
      {conditionKinds.map((option) => <option key={option} value={option}>{conditionLabels[option]}</option>)}
    </Select></Field>
    {(kind === "visible" || kind === "hidden" || kind === "text_contains") && (
      <Field label="Condition selector">
        <input className="mono" required value={value.selector ?? ""} onChange={(event) => onChange({ ...value, selector: event.target.value })} />
      </Field>
    )}
    {(kind === "text_contains" || kind === "url_contains") && (
      <Field label={kind === "url_contains" ? "URL contains" : "Text contains"}>
        <input required value={value.text ?? ""} onChange={(event) => onChange({ ...value, text: event.target.value })} />
      </Field>
    )}
    {kind === "value_equals" && <>
      <Field label="Workflow value name">
        <input className="mono" required value={value.value_ref ?? ""} onChange={(event) => onChange({ ...value, value_ref: event.target.value })} />
      </Field>
      <Field label="Compare as"><Select value={scalarType} onChange={(event) => {
        const type = event.target.value as WorkflowInputDefinition["type"];
        setScalarType(type);
        onChange({ ...value, value: type === "number" ? 0 : type === "boolean" ? false : "" });
      }}>
        {workflowInputTypes.map((option) => <option key={option} value={option}>{option}</option>)}
      </Select></Field>
      <ScalarField label="Expected value" type={scalarType} value={value.value} onChange={(next) => onChange({
        ...value, value: next,
      })} />
    </>}
  </div>;
}

function NavigationFields({ step, change }: { step: DraftStep; change: (step: DraftStep) => void }) {
  const parameters = workflowFixtureParameters(step.url);
  const preview = step.fixture_params && workflowUrlPreview(step.url, step.fixture_params);
  const nonempty = step.fixture_params && Object.values(step.fixture_params).every((value) => value.trim());
  return <>
    <Field label="Navigation path or URL" hint="Use a relative path or approved URL. A named parameter occupies a complete query value.">
      <input required value={step.url ?? ""} className="mono" placeholder="/search?term=[QUERY]" onChange={(event) => {
        const url = event.target.value;
        const current = step.fixture_params;
        change({
          ...step,
          url,
          fixture_params: current && Object.fromEntries(workflowFixtureParameters(url).map((name) => [
            name, Object.hasOwn(current, name) ? current[name] : "",
          ])),
        });
      }} />
    </Field>
    {step.action === "navigate" && <Field label="Wait after navigation (seconds)" hint="Leave blank or enter 0 to skip the wait.">
      <input type="number" min={0} max={postNavigationWaitMaxMs / 1000} step={0.001} placeholder="0"
        value={step.post_navigation_wait_ms == null ? "" : step.post_navigation_wait_ms / 1000}
        onChange={(event) => change({
          ...step,
          post_navigation_wait_ms: event.target.value ? Math.round(event.target.valueAsNumber * 1000) : undefined,
        })}
      />
    </Field>}
    {(parameters.length > 0 || step.fixture_params != null) && <div className="stack compact-stack">
      <label className="checkbox-line">
        <input type="checkbox" checked={step.fixture_params != null} onChange={(event) => change({
          ...step, fixture_params: event.target.checked ? Object.fromEntries(parameters.map((name) => [name, ""])) : undefined,
        })} />
        Use named URL parameters
      </label>
      {step.fixture_params && parameters.map((name) => (
        <Field key={name} label={name + " default"}>
          <input required maxLength={fixtureParameterMaxLength * 2}
            value={Object.hasOwn(step.fixture_params ?? {}, name) ? step.fixture_params?.[name] : ""}
            onChange={(event) => change({ ...step, fixture_params: { ...step.fixture_params, [name]: event.target.value } })}
          />
        </Field>
      ))}
      {preview?.url && <WorkflowUrlPreview label="URL with saved defaults" url={preview.url} />}
      {nonempty && preview?.error && <ErrorNotice error={preview.error} />}
    </div>}
  </>;
}

function ActionValueFields({
  step, structured, change,
}: {
  step: DraftStep;
  structured: boolean;
  change: (step: DraftStep) => void;
}) {
  const source = step.secret_ref != null ? "secret" : step.value_ref != null ? "reference" : "literal";
  const label = step.action === "assert_text" || step.action === "wait_for_text" ? "Expected text" :
    step.action === "press" ? "Key or shortcut" : literalOnly.has(step.action) ? "URL contains" : "Value";
  return <>
    {structured && !literalOnly.has(step.action) && <Field label="Value source">
      <Select value={source} onChange={(event) => change({
        ...step, value: event.target.value === "literal" ? "" : undefined,
        value_ref: event.target.value === "reference" ? "" : undefined,
        secret_ref: event.target.value === "secret" ? "" : undefined,
      })}>
        <option value="literal">Entered value</option>
        <option value="reference">Workflow input or captured value</option>
        {step.action === "fill" && <option value="secret">Secret reference</option>}
      </Select>
    </Field>}
    {(source === "literal" || !structured) && <Field label={label}>
      <input value={step.value ?? ""} disabled={Boolean(step.secret_ref)}
        placeholder={step.action === "press" ? "Enter" : undefined}
        onChange={(event) => change({ ...step, value: event.target.value })}
      />
    </Field>}
    {structured && source === "reference" && <Field label="Workflow value name">
      <input className="mono" required value={step.value_ref ?? ""} onChange={(event) => change({ ...step, value_ref: event.target.value })} />
    </Field>}
    {(source === "secret" || !structured && step.action === "fill") && <Field label="Secret reference">
      <input className="mono" placeholder="CHECKOUT_TEST_EMAIL" required={structured}
        value={step.secret_ref ?? ""}
        onChange={(event) => change({
          ...step, value: event.target.value ? undefined : step.value,
          secret_ref: event.target.value || (structured ? "" : undefined),
        })}
      />
    </Field>}
  </>;
}

function BlockReference({
  token, step, blocks, change,
}: {
  token: string;
  step: DraftStep;
  blocks: WorkflowBlock[];
  change: (step: DraftStep) => void;
}) {
  const selected = blocks.find((block) => block.id === step.block_id);
  const versions = useQuery({
    queryKey: platformKey(token, "workflow-block-versions", step.block_id ?? ""),
    queryFn: () => api<WorkflowBlock[]>("/workflow-blocks/" + step.block_id + "/versions", token),
    enabled: Boolean(step.block_id),
  });
  const allVersions = [
    ...(versions.data?.map((block) => block.version) ?? []),
    ...(selected ? [selected.version] : []), ...(step.block_version ? [step.block_version] : []),
  ];
  return <>
    <Field label="Reusable block"><Select required value={step.block_id ?? ""} onChange={(event) => {
      const block = blocks.find((item) => item.id === event.target.value);
      change({ ...step, block_id: block?.id, block_version: block?.version, steps: undefined });
    }}>
      <option value="">Choose a project block</option>
      {step.block_id && !selected && <option value={step.block_id}>Saved block ({step.block_id})</option>}
      {blocks.map((block) => <option key={block.id} value={block.id}>{block.name}</option>)}
    </Select></Field>
    <Field label="Pinned block revision"><Select required value={step.block_version ?? ""} onChange={(event) => change({
      ...step, block_version: Number(event.target.value), steps: undefined,
    })}>
      <option value="">Choose a revision</option>
      {[...new Set(allVersions)].sort((a, b) => b - a).map((version) => <option key={version} value={version}>Version {version}</option>)}
    </Select></Field>
    <ErrorNotice error={versions.error} />
  </>;
}

interface StepListProps {
  token: string;
  steps: DraftStep[];
  onChange: (steps: DraftStep[]) => void;
  allocate: () => number;
  structured: boolean;
  blocks: WorkflowBlock[];
  issues: ValidationIssue[];
  location?: Location;
  labelPrefix?: string;
  allowEmpty?: boolean;
}

function newAction(step: DraftStep, action: Step["action"], index: number): DraftStep {
  return {
    key: step.key,
    action,
    timeout_ms: navigationActions.has(step.action) === navigationActions.has(action) ? step.timeout_ms : undefined,
    ...(action === "navigate" || action === "open_tab" ? { url: "/" } : {}),
    ...(action === "checkpoint" ? { name: "checkpoint-" + (index + 1) } : {}),
    ...(action === "if" ? { condition: { kind: "visible" }, steps: [] } : {}),
    ...(action === "repeat" ? { max_iterations: 2, steps: [] } : {}),
    ...(valueActions.has(action) ? { value: "" } : {}),
  };
}

export function WorkflowStepList({
  token, steps, onChange, allocate, structured, blocks, issues, location = ["steps"], labelPrefix = "", allowEmpty = false,
}: StepListProps) {
  const actionsForVersion = structured ? actions : legacyActions;
  return <div className="step-list">
    {steps.map((step, index) => {
      const stepLabel = labelPrefix + (index + 1);
      const position = [...location, index];
      const ownIssues = issuesAt(issues, position, true);
      const action = step.action;
      const change = (next: DraftStep) => onChange(steps.map((entry, at) => at === index ? next : entry));
      const move = (delta: -1 | 1) => {
        const next = [...steps];
        [next[index], next[index + delta]] = [next[index + delta], next[index]];
        onChange(next);
      };
      const body = (key: "steps" | "else_steps", title: string, prefix: string) => (
        <fieldset className="choice-fieldset stack compact-stack">
          <legend>{title}</legend>
          <WorkflowStepList {...{ token, allocate, structured, blocks, issues }}
            steps={step[key] ?? []} onChange={(children) => change({ ...step, [key]: children })}
            location={[...position, key]} labelPrefix={prefix} allowEmpty={key === "else_steps"} />
        </fieldset>
      );
      const usesSelector = !withoutSelector.has(action) && !controlActions.has(action);
      const usesFrame = usesSelector || action === "if" || action === "repeat";
      const isNavigation = navigationActions.has(action);
      return <fieldset className="step-card" key={step.key}>
        <div className="step-header" style={{ gridTemplateColumns: "max-content minmax(0, 1fr) auto" }}>
          <span className="step-index" aria-hidden="true">{stepLabel}</span>
          <Select aria-label={"Step " + stepLabel + " action"} value={action}
            onChange={(event) => change(newAction(step, event.target.value as Step["action"], index))}>
            {actionsForVersion.map((value) => <option key={value} value={value}>{actionLabels[value]}</option>)}
          </Select>
          <div className="step-controls">
            <button type="button" className="icon-button bare" title="Move up"
              aria-label={"Move step " + stepLabel + " up"} disabled={index === 0} onClick={() => move(-1)}><ArrowUp size={16} /></button>
            <button type="button" className="icon-button bare" title="Move down"
              aria-label={"Move step " + stepLabel + " down"} disabled={index === steps.length - 1} onClick={() => move(1)}><ArrowDown size={16} /></button>
            {structured && <button type="button" className="icon-button bare" title="Duplicate"
              aria-label={"Duplicate step " + stepLabel} onClick={() => {
                const [duplicate] = draftSteps(serializeSteps([step]), allocate);
                onChange([...steps.slice(0, index + 1), duplicate, ...steps.slice(index + 1)]);
              }}><Copy size={16} /></button>}
            <button type="button" className="icon-button bare danger" title="Remove"
              aria-label={"Remove step " + stepLabel} disabled={!allowEmpty && steps.length === 1}
              onClick={() => onChange(steps.filter((_, at) => at !== index))}><Trash2 size={16} /></button>
          </div>
        </div>
        <div className="form-grid">
          {isNavigation && <NavigationFields step={step} change={change} />}
          {usesSelector && <Field label={action === "expect_popup" ? "Popup opener selector" : "Element selector"}>
            <input required className="mono" placeholder="[data-testid='submit']"
              value={step.selector ?? ""} onChange={(event) => change({ ...step, selector: event.target.value })} />
          </Field>}
          {action === "checkpoint" && <Field label="Checkpoint name">
            <input required maxLength={64} value={step.name ?? ""} onChange={(event) => change({ ...step, name: event.target.value })} />
          </Field>}
          {tabActions.has(action) && <Field label="Tab name" hint={action === "switch_tab" ? "Use main to return to the first tab." : undefined}>
            <input required maxLength={32} className="mono" value={step.tab ?? ""} onChange={(event) => change({ ...step, tab: event.target.value })} />
          </Field>}
          {action !== "checkpoint" && !controlActions.has(action) && (
            <Field label={isNavigation ? "Initial navigation timeout (ms)" : "Timeout in milliseconds"}
              hint={isNavigation ? "Leave blank to use the workflow or proxy default." : undefined}>
              <input type="number" min={100} max={isNavigation || structured ? navigationTimeoutMaxMs : 15_000}
                placeholder={isNavigation ? undefined : "5000"}
                value={step.timeout_ms ?? ""} onChange={(event) => change({
                  ...step, timeout_ms: event.target.value ? event.target.valueAsNumber : undefined,
                })} />
            </Field>
          )}
          {valueActions.has(action) && <ActionValueFields step={step} structured={structured} change={change} />}
          {structured && action === "extract" && <Field label="Save as workflow value" hint="A capture is available to later steps in this attempt.">
            <input className="mono" maxLength={32} value={step.save_as ?? ""} onChange={(event) => change({ ...step, save_as: event.target.value || undefined })} />
          </Field>}
          {structured && usesFrame && <Field label="Frame selectors (optional)" hint="One CSS selector per line, outermost frame first.">
            <textarea rows={2} className="mono" value={step.frame?.join("\n") ?? ""} onChange={(event) => change({
              ...step, frame: event.target.value ? event.target.value.split("\n") : undefined,
            })} />
          </Field>}
          {action === "repeat" && <Field label="Maximum repetitions">
            <input type="number" min={1} max={100} required value={step.max_iterations ?? ""} onChange={(event) => change({
              ...step, max_iterations: event.target.value ? event.target.valueAsNumber : undefined,
            })} />
          </Field>}
          {action === "use_block" && <BlockReference {...{ token, step, blocks, change }} />}
        </div>
        {action === "repeat" && <label className="checkbox-line">
          <input type="checkbox" checked={Boolean(step.condition)} onChange={(event) => change({
            ...step, condition: event.target.checked ? { kind: "visible" } : undefined,
          })} />
          Stop repeating when a condition matches
        </label>}
        {(action === "if" || action === "repeat") && step.condition && <ConditionEditor
          value={step.condition} onChange={(condition) => change({ ...step, condition })}
        />}
        {action === "if" && body("steps", "When the condition matches", stepLabel + ".")}
        {action === "if" && body("else_steps", "Otherwise", stepLabel + ".else.")}
        {action === "repeat" && body("steps", "Repeat these steps", stepLabel + ".")}
        {action === "assert_not_visible" && <p className="muted small-text">Fails if the element is still visible at the deadline.</p>}
        {action === "checkpoint" && <p className="muted small-text">
          {structured ? "Records a point in the timeline. Existing action history stays attached to the attempt." : "Place directly after a successful assertion. This records a progress boundary and keeps the existing action history."}
        </p>}
        <ErrorNotice error={showIssues(ownIssues)} />
      </fieldset>;
    })}
    <button type="button" className="button add-step" onClick={() => onChange([
      ...steps, { key: allocate(), action: "click" },
    ])}><Plus size={16} /> {labelPrefix ? "Add nested step" : "Add step"}</button>
  </div>;
}
