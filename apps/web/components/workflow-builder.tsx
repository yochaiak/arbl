"use client";

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Blocks, Download, Plus, Trash2, Upload } from "lucide-react";
import { ZodError } from "zod";
import {
  ApiError,
  api,
  navigationTimeoutMaxMs,
  saveFile,
  workflowInputTypes,
  workflowSchema,
  type Step,
  type ValidationIssue,
  type Workflow,
  type WorkflowBlock,
  type WorkflowCondition,
  type WorkflowInput,
  type WorkflowInputDefinition,
} from "@/lib/api";
import { platformKey } from "@/lib/platform";
import { Modal } from "./platform-ui";
import { ErrorNotice, Field, Loading, SectionTitle, Select } from "./ui";
import {
  ConditionEditor,
  draftSteps,
  issuesAt,
  ScalarField,
  serializeSteps,
  showIssues,
  WorkflowStepList,
} from "./workflow-step-editor";

const initialSteps: Step[] = [{ action: "navigate", url: "/" }];
type InputDraft = WorkflowInputDefinition & { key: number; name: string };

function readIssues(error: unknown): ValidationIssue[] {
  if (error instanceof ApiError) return error.issues;
  if (error instanceof ZodError) return error.issues.map(({ path, message }) => ({
    path: path.filter((part): part is string | number => typeof part === "string" || typeof part === "number"),
    message,
  }));
  return [];
}

function draftWorkflow(workflow: WorkflowInput | null) {
  let nextKey = 0;
  const steps = draftSteps(workflow?.steps ?? initialSteps, () => nextKey++);
  const inputs = Object.entries(workflow?.inputs ?? {}).map(([name, definition]) => ({
    key: nextKey++, name, ...definition,
  }));
  return { steps, inputs, nextKey };
}

function finalLegacyCheck(step: Step | undefined): WorkflowCondition | null {
  if (!step?.selector) return null;
  if (step.action === "assert_visible") return { kind: "visible", selector: step.selector };
  if (step.action === "assert_not_visible") return { kind: "hidden", selector: step.selector };
  if (step.action === "assert_text" && step.value?.trim()) {
    return { kind: "text_contains", selector: step.selector, text: step.value };
  }
  return null;
}

function WorkflowInputsEditor({
  value, onChange, allocate, issues,
}: {
  value: InputDraft[];
  onChange: (inputs: InputDraft[]) => void;
  allocate: () => number;
  issues: ValidationIssue[];
}) {
  return <fieldset className="choice-fieldset stack compact-stack">
    <legend>Workflow inputs</legend>
    {value.map((entry, index) => {
      const change = (next: InputDraft) => onChange(value.map((item, at) => at === index ? next : item));
      return <div className="step-card" key={entry.key}>
        <div className="form-grid">
          <Field label="Input name"><input
            required className="mono" maxLength={32} pattern="[A-Za-z][A-Za-z0-9_]*"
            value={entry.name} onChange={(event) => change({ ...entry, name: event.target.value })}
          /></Field>
          <Field label="Input type"><Select value={entry.type} onChange={(event) => change({
            ...entry,
            type: event.target.value as WorkflowInputDefinition["type"],
            default: undefined,
          })}>
            {workflowInputTypes.map((type) => <option key={type} value={type}>{type}</option>)}
          </Select></Field>
          <ScalarField label="Default value" type={entry.type} value={entry.default}
            onChange={(next) => change({ ...entry, default: next })} />
        </div>
        <div className="actions">
          <label className="checkbox-line">
            <input type="checkbox" checked={entry.required ?? true} onChange={(event) => change({
              ...entry, required: event.target.checked,
            })} />
            Require a value
          </label>
          {entry.default != null && <button type="button" className="button subtle" onClick={() => change({
            ...entry, default: undefined,
          })}>Clear default</button>}
          <button type="button" className="icon-button bare danger" aria-label={"Remove input " + (index + 1)}
            onClick={() => onChange(value.filter((_, at) => at !== index))}><Trash2 size={16} /></button>
        </div>
        <ErrorNotice error={showIssues(issuesAt(issues, ["inputs", entry.name]))} />
      </div>;
    })}
    <button type="button" className="button add-step" onClick={() => onChange([
      ...value, { key: allocate(), name: "", type: "string", required: true },
    ])}><Plus size={16} /> Add workflow input</button>
  </fieldset>;
}

function ProjectBlockEditor({
  token, initial, library, onSaved,
}: {
  token: string;
  initial?: WorkflowBlock;
  library: WorkflowBlock[];
  onSaved: (block: WorkflowBlock) => void;
}) {
  const [initialDraft] = useState(() => draftWorkflow(initial ?? null));
  const [name, setName] = useState(initial?.name ?? "");
  const [steps, setSteps] = useState(initialDraft.steps);
  const key = useRef(initialDraft.nextKey);
  const save = useMutation({
    mutationFn: () => api<WorkflowBlock>(
      initial ? "/workflow-blocks/" + initial.id : "/workflow-blocks", token, {
        method: initial ? "PUT" : "POST",
        body: { name: name.trim(), steps: serializeSteps(steps) },
      },
    ),
    onSuccess: onSaved,
  });
  const issues = readIssues(save.error);
  return <form className="modal-body stack" onSubmit={(event) => {
    event.preventDefault();
    save.mutate();
  }}>
    <Field label="Block name"><input required maxLength={100} value={name} onChange={(event) => setName(event.target.value)} /></Field>
    <WorkflowStepList
      token={token} steps={steps} onChange={setSteps} allocate={() => key.current++}
      structured blocks={library} issues={issues}
    />
    <ErrorNotice error={save.error} />
    <div className="actions">
      <button type="submit" className="button primary" disabled={save.isPending}>
        {save.isPending ? "Saving…" : initial ? "Save block revision" : "Save new block"}
      </button>
    </div>
  </form>;
}

function ProjectBlockLibrary({ token, close }: { token: string; close: () => void }) {
  const [selected, setSelected] = useState("");
  const query = useQueryClient();
  const key = platformKey(token, "workflow-blocks");
  const library = useQuery({
    queryKey: key,
    queryFn: () => api<WorkflowBlock[]>("/workflow-blocks", token),
  });
  const records = library.data ?? [];
  const initial = records.find((entry) => entry.id === selected);
  return <Modal title="Project workflow blocks" detail="Share reusable steps between workflows. Saved workflows pin the selected block revision." close={close} wide>
    <div className="modal-body stack compact-stack">
      <Field label="Edit workflow block"><Select value={selected} onChange={(event) => setSelected(event.target.value)}>
        <option value="">Create a new block</option>
        {records.map((block) => <option value={block.id} key={block.id}>{block.name} · v{block.version}</option>)}
      </Select></Field>
      {library.isPending && <Loading />}
      <ErrorNotice error={library.error} />
    </div>
    <ProjectBlockEditor key={(initial?.id ?? "new") + ":" + (initial?.version ?? 0)}
      token={token} initial={initial} library={records} onSaved={(block) => {
        query.setQueryData<WorkflowBlock[]>(key, (previous = []) => [
          ...previous.filter((entry) => entry.id !== block.id), block,
        ]);
        void query.invalidateQueries({ queryKey: key });
        void query.invalidateQueries({ queryKey: platformKey(token, "workflow-block-versions", block.id) });
        setSelected(block.id);
      }} />
  </Modal>;
}

export function WorkflowEditor({
  token,
  initial,
  onSaved,
  onPreview,
}: {
  token: string;
  initial: Workflow | null;
  onSaved: (workflow: Workflow) => void;
  onPreview: (workflow: Workflow) => void;
}) {
  const [initialDraft] = useState(() => draftWorkflow(initial));
  const [name, setName] = useState(initial?.name ?? "");
  const [structured, setStructured] = useState(!initial || initial.schema_version === 2);
  const [steps, setSteps] = useState(initialDraft.steps);
  const [initialNavigationTimeout, setInitialNavigationTimeout] = useState(initial?.initial_navigation_timeout_ms ?? undefined);
  const [inputs, setInputs] = useState(initialDraft.inputs);
  const [successCheck, setSuccessCheck] = useState<WorkflowCondition | null>(initial?.success_check ?? null);
  const [blocksOpen, setBlocksOpen] = useState(false);
  const [importError, setImportError] = useState<Error | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const nextKey = useRef(initialDraft.nextKey);
  const allocate = () => nextKey.current++;
  const query = useQueryClient();
  const blocks = useQuery({
    queryKey: platformKey(token, "workflow-blocks"),
    queryFn: () => api<WorkflowBlock[]>("/workflow-blocks", token),
    enabled: structured,
  });

  function payload(): WorkflowInput {
    const body = { name, steps: serializeSteps(steps), initial_navigation_timeout_ms: initialNavigationTimeout };
    if (!structured) return body;
    const definitions = inputs.map(({ name, key, ...definition }) => {
      void key;
      return [name.trim(), definition] as const;
    });
    if (new Set(definitions.map(([name]) => name)).size !== definitions.length)
      throw new Error("Use a different name for each workflow input.");
    return {
      ...body,
      schema_version: 2,
      inputs: Object.fromEntries(definitions),
      success_check: successCheck,
    };
  }

  const save = useMutation({
    mutationFn: async (preview: boolean) => {
      const data = workflowSchema.parse(payload());
      const workflow = await api<Workflow>(initial ? "/workflows/" + initial.id : "/workflows", token, {
        method: initial ? "PUT" : "POST",
        body: data,
      });
      return { workflow, preview };
    },
    onSuccess: ({ workflow, preview }) => {
      query.setQueryData<Workflow[]>(["workflows", token], (list = []) => [
        ...list.filter((entry) => entry.id !== workflow.id), workflow,
      ]);
      void query.invalidateQueries({ queryKey: ["workflows", token] });
      onSaved(workflow);
      if (preview) onPreview(workflow);
    },
  });
  const issues = readIssues(save.error);

  async function load(file: File) {
    try {
      const workflow = workflowSchema.parse(JSON.parse(await file.text()));
      setName(workflow.name);
      setStructured(workflow.schema_version === 2);
      setSteps(draftSteps(workflow.steps, allocate));
      setInitialNavigationTimeout(workflow.initial_navigation_timeout_ms ?? undefined);
      setInputs("inputs" in workflow ? Object.entries(workflow.inputs ?? {}).map(([name, definition]) => ({
        key: allocate(), name, ...definition,
      })) : []);
      setSuccessCheck("success_check" in workflow ? workflow.success_check ?? null : null);
      save.reset();
      setImportError(null);
    } catch (error) {
      setImportError(error instanceof ZodError ? new Error(showIssues(readIssues(error))) :
        error instanceof Error ? error : new Error("The workflow JSON could not be read."));
    }
  }

  function download() {
    try {
      saveFile(new Blob([JSON.stringify(payload(), null, 2)], { type: "application/json" }), "workflow.json");
      setImportError(null);
    } catch (error) {
      setImportError(error instanceof Error ? error : new Error("The workflow could not be exported."));
    }
  }

  return <>
    <form className="panel edit-form" onSubmit={(event) => {
      event.preventDefault();
      save.mutate(false);
    }}>
      <SectionTitle title={initial ? "Edit workflow" : "Build a workflow"}
        detail="Define browser steps and the checks that confirm success." />
      <div className="form-grid workflow-top">
        <Field label="Workflow name"><input required maxLength={100} placeholder="Checkout confirmation"
          value={name} onChange={(event) => setName(event.target.value)} /></Field>
        <div className="actions">
          <button type="button" className="button secondary" onClick={() => input.current?.click()}>
            <Upload size={16} /> Import
          </button>
          <input className="sr-only" tabIndex={-1} aria-label="Import workflow JSON" ref={input}
            type="file" accept=".json,application/json" onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void load(file);
              event.target.value = "";
            }} />
          <button type="button" className="button secondary" onClick={download}><Download size={16} /> Export</button>
          <button type="button" className="button secondary" onClick={() => setBlocksOpen(true)}>
            <Blocks size={16} /> Workflow blocks
          </button>
        </div>
      </div>
      {!structured && <div className="actions">
        <button type="button" className="button secondary" onClick={() => {
          setStructured(true);
          setSuccessCheck(finalLegacyCheck(steps.at(-1)));
          save.reset();
        }}>Enable workflow inputs and branching</button>
      </div>}
      <ErrorNotice error={importError} />
      <Field label="Default initial navigation timeout (ms)"
        hint="Applies to navigation steps without a timeout. Leave blank to use the proxy policy or connection default.">
        <input type="number" min={100} max={navigationTimeoutMaxMs} value={initialNavigationTimeout ?? ""}
          onChange={(event) => setInitialNavigationTimeout(event.target.value ? event.target.valueAsNumber : undefined)} />
      </Field>
      {structured && <WorkflowInputsEditor value={inputs} onChange={setInputs} allocate={allocate} issues={issues} />}
      <WorkflowStepList
        token={token} steps={steps} onChange={setSteps} allocate={allocate}
        structured={structured} blocks={blocks.data ?? []} issues={issues}
      />
      {structured && <fieldset className="choice-fieldset stack compact-stack">
        <legend>Success check</legend>
        <label className="checkbox-line">
          <input type="checkbox" checked={Boolean(successCheck)} onChange={(event) => setSuccessCheck(
            event.target.checked ? { kind: "visible" } : null,
          )} />
          Verify the result after the last step
        </label>
        {successCheck ? <ConditionEditor value={successCheck} onChange={setSuccessCheck} /> :
          <p className="muted small-text">Automated sessions can finish once their steps complete. Manual Chrome requires a success check.</p>}
        <ErrorNotice error={showIssues(issuesAt(issues, ["success_check"]))} />
      </fieldset>}
      <ErrorNotice error={issues.length ? showIssues(issues) : save.error} />
      <div className="form-footer">
        <button type="button" className="button secondary" disabled={save.isPending} onClick={(event) => {
          if (event.currentTarget.form?.reportValidity()) save.mutate(true);
        }}>Save and preview</button>
        <button className="button primary" disabled={save.isPending}>
          {save.isPending ? "Saving…" : initial ? "Save new version" : "Save workflow"}
        </button>
      </div>
    </form>
    {blocksOpen && <ProjectBlockLibrary token={token} close={() => setBlocksOpen(false)} />}
  </>;
}
