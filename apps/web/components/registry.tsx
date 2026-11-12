"use client";

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Check, Copy, Download, Globe2, Plus, Route, ShieldCheck, Trash2, Upload } from "lucide-react";
import {
  actions, actionLabels, api, label, saveFile, workflowSchema,
  type Step, type Target, type TargetInput, type Workflow, type WorkflowInput,
} from "@/lib/api";
import { Badge, Empty, ErrorNotice, Field, SectionTitle, Select } from "./ui";

const blankTarget: TargetInput = {
  name: "", origin: "", resource_origins: [], mode: "browser_only", rate_limit: 6, session_policy: "fresh", authorized: false,
};

function TargetEditor({ token, initial, onSaved }: { token: string; initial: Target | null; onSaved: (target: Target) => void }) {
  const [values, setValues] = useState<TargetInput>(initial ? {
    name: initial.name,
    origin: initial.origin,
    resource_origins: initial.resource_origins ?? [],
    mode: initial.mode,
    rate_limit: initial.rate_limit,
    session_policy: initial.session_policy,
    authorized: initial.authorized,
  } : blankTarget);
  const [resourceText, setResourceText] = useState(values.resource_origins.join("\n"));
  const query = useQueryClient();
  const save = useMutation({
    mutationFn: () => api<Target>(initial ? `/targets/${initial.id}` : "/targets", token, {
      method: initial ? "PUT" : "POST",
      body: { ...values, name: values.name.trim(), origin: values.origin.trim(), resource_origins: resourceText.split(/\s+/).filter(Boolean) },
    }),
    onSuccess: (target) => {
      query.setQueryData<Target[]>(["targets", token], (list = []) => [
        ...list.filter((entry) => entry.id !== target.id), target,
      ]);
      void query.invalidateQueries({ queryKey: ["targets", token] });
      onSaved(target);
    },
  });

  function set<K extends keyof TargetInput>(key: K, value: TargetInput[K]) {
    setValues((previous) => ({ ...previous, [key]: value }));
  }

  return <form className="panel edit-form" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
    <SectionTitle title={initial ? "Edit site" : "Register a site"} detail={initial ? "Saving creates a new version for future runs." : "Choose a site you control or have permission to test."} />
    <div className="form-grid">
      <Field label="Site name"><input required maxLength={100} value={values.name} placeholder="Checkout staging" onChange={(event) => set("name", event.target.value)} /></Field>
      <Field label="Navigation origin" hint="Scheme, hostname, and port, with no path."><input required type="url" value={values.origin} placeholder="https://staging.example.com" onChange={(event) => set("origin", event.target.value)} /></Field>
      <Field label="Integration mode" hint="Instrumented sites can submit separate manual diagnostics."><Select value={values.mode} onChange={(event) => set("mode", event.target.value as TargetInput["mode"])}><option value="browser_only">Browser-only</option><option value="instrumented">Instrumented</option></Select></Field>
      <Field label="Session policy"><Select value={values.session_policy} onChange={(event) => set("session_policy", event.target.value as TargetInput["session_policy"])}><option value="fresh">Fresh session each trial</option><option value="repeat_within_trial">Repeat within a trial</option></Select></Field>
      <Field label="Trial starts per minute"><input required type="number" min={1} max={60} value={values.rate_limit} onChange={(event) => set("rate_limit", event.target.valueAsNumber)} /></Field>
    </div>
    <Field label="Resource origins" hint="Optional. Enter one origin per line for scripts, styles, and other subresources.">
      <textarea rows={3} spellCheck={false} className="mono" value={resourceText} onChange={(event) => setResourceText(event.target.value)} />
    </Field>
    <label className="check"><input required type="checkbox" checked={values.authorized} onChange={(event) => set("authorized", event.target.checked)} /><span>I control this site or have permission to run these workflows.</span></label>
    <ErrorNotice error={save.error} />
    <div className="form-footer"><button className="button primary" disabled={save.isPending}>{save.isPending ? "Saving…" : initial ? "Save new version" : "Register site"}</button></div>
  </form>;
}

function SiteProof({ site, token }: { site: Target; token: string }) {
  const query = useQueryClient();
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<Error | null>(null);
  const verify = useMutation({
    mutationFn: () => api(`/targets/${site.id}/verify`, token, { method: "POST" }),
    onSuccess: () => { void query.invalidateQueries({ queryKey: ["targets"] }); },
  });
  if (!site.proof_token || site.verified) return null;

  return <section className="panel proof">
    <SectionTitle title="Verify site ownership" detail="Serve this marker as plain text at the path below, then verify the site." />
    <code className="proof-value">{site.proof_path || "/.well-known/abrl-target.txt"}</code>
    <div className="proof-row"><code className="proof-value">{site.proof_token}</code>
      <button type="button" className="button secondary" onClick={async () => {
        try { await navigator.clipboard.writeText(site.proof_token!); setCopied(true); setCopyError(null); }
        catch (error) { setCopyError(error instanceof Error ? error : new Error("Could not copy the marker.")); }
      }}>{copied ? <Check size={16} /> : <Copy size={16} />}{copied ? "Copied" : "Copy"}</button></div>
    <ErrorNotice error={verify.error || copyError} />
    <button className="button secondary" disabled={verify.isPending} onClick={() => verify.mutate()}><ShieldCheck size={17} />{verify.isPending ? "Verifying…" : "Verify site"}</button>
  </section>;
}

export function SiteRegistry({ token, targets }: { token: string; targets: Target[] }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [editorRevision, setEditorRevision] = useState(0);
  const editing = targets.find((target) => target.id === selected) ?? null;
  function choose(id: string | null) {
    setSelected(id);
    setEditorRevision((revision) => revision + 1);
  }
  return <div className="registry-layout">
    <section className="panel records">
      <SectionTitle title="Registered sites" action={<button type="button" className="icon-button" aria-label="New site" title="New site" onClick={() => choose(null)}><Plus size={18} /></button>} />
      {targets.length === 0 ? <Empty title="No sites registered" detail="Register a site to make it available to your workflows." /> : <div className="record-list">
        {targets.map((target) => <button key={target.id} type="button" className={`record ${selected === target.id ? "selected" : ""}`} onClick={() => choose(target.id)}>
          <span className="record-icon"><Globe2 size={18} /></span><span className="record-text"><strong>{target.name}</strong><span>{target.origin}</span></span><span className="version">v{target.version}</span>
        </button>)}
      </div>}
    </section>
    <div className="stack">
      <TargetEditor key={editorRevision} token={token} initial={editing} onSaved={(site) => choose(site.id)} />
      {editing && <SiteProof key={editing.id} token={token} site={editing} />}
    </div>
  </div>;
}

type DraftStep = Step & { key: number };
const defaultSteps: Step[] = [
  { action: "navigate", url: "/" },
  { action: "assert_visible", selector: "h1" },
];

function StepEditor({ step, index, length, onChange, onMove, onRemove }: {
  step: DraftStep;
  index: number;
  length: number;
  onChange: (step: DraftStep) => void;
  onMove: (delta: -1 | 1) => void;
  onRemove: () => void;
}) {
  const inputValue = ["fill", "select", "assert_text"].includes(step.action);
  return <fieldset className="step-card">
    <div className="step-header">
      <span className="step-index">{String(index + 1).padStart(2, "0")}</span>
      <Select aria-label={`Step ${index + 1} action`} value={step.action} onChange={(event) => {
        const action = event.target.value as Step["action"];
        onChange({ key: step.key, action, url: action === "navigate" ? "/" : undefined, selector: action === "navigate" ? undefined : step.selector, timeout_ms: step.timeout_ms });
      }}>{actions.map((action) => <option key={action} value={action}>{actionLabels[action]}</option>)}</Select>
      <div className="step-controls">
        <button type="button" className="icon-button bare" title="Move up" aria-label={`Move step ${index + 1} up`} disabled={index === 0} onClick={() => onMove(-1)}><ArrowUp size={16} /></button>
        <button type="button" className="icon-button bare" title="Move down" aria-label={`Move step ${index + 1} down`} disabled={index === length - 1} onClick={() => onMove(1)}><ArrowDown size={16} /></button>
        <button type="button" className="icon-button bare danger" title="Remove step" aria-label={`Remove step ${index + 1}`} onClick={onRemove}><Trash2 size={16} /></button>
      </div>
    </div>
    <div className="form-grid">
      {step.action === "navigate" ? <Field label="Site-relative path"><input required value={step.url ?? ""} className="mono" placeholder="/example" onChange={(event) => onChange({ ...step, url: event.target.value })} /></Field>
      : <Field label="Element selector"><input required value={step.selector ?? ""} className="mono" placeholder="[data-testid='submit']" onChange={(event) => onChange({ ...step, selector: event.target.value })} /></Field>}
      <Field label="Timeout in milliseconds"><input type="number" min={100} max={15000} placeholder="5000" value={step.timeout_ms ?? ""} onChange={(event) => onChange({ ...step, timeout_ms: event.target.value ? event.target.valueAsNumber : undefined })} /></Field>
      {inputValue && <Field label={step.action === "assert_text" ? "Expected text" : "Value"}>
        <input value={step.value ?? ""} disabled={Boolean(step.secret_ref)} onChange={(event) => onChange({ ...step, value: event.target.value })} />
      </Field>}
      {step.action === "fill" && <Field label="Secret reference"><input value={step.secret_ref ?? ""} className="mono" placeholder="CHECKOUT_TEST_EMAIL" onChange={(event) => onChange({ ...step, value: event.target.value ? undefined : step.value, secret_ref: event.target.value || undefined })} /></Field>}
    </div>
  </fieldset>;
}

function WorkflowEditor({ token, initial, onSaved, onPreview }: {
  token: string;
  initial: Workflow | null;
  onSaved: (workflow: Workflow) => void;
  onPreview: (workflow: Workflow) => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [steps, setSteps] = useState<DraftStep[]>(() => (initial?.steps ?? defaultSteps).map((step, index) => ({ ...step, key: index })));
  const [importError, setImportError] = useState<Error | null>(null);
  const nextKey = useRef(steps.length);
  const input = useRef<HTMLInputElement>(null);
  const query = useQueryClient();
  const payload = (): WorkflowInput => ({
    name,
    steps: steps.map(({ key, ...step }) => { void key; return step; }),
  });
  const save = useMutation({
    mutationFn: (preview: boolean) => {
      const data = workflowSchema.parse(payload());
      return api<Workflow>(initial ? `/workflows/${initial.id}` : "/workflows", token, {
        method: initial ? "PUT" : "POST", body: data,
      }).then((workflow) => ({ workflow, preview }));
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

  function move(index: number, delta: -1 | 1) {
    setSteps((previous) => {
      const next = [...previous];
      [next[index], next[index + delta]] = [next[index + delta], next[index]];
      return next;
    });
  }

  async function importWorkflow(file: File) {
    try {
      if (file.size > 250_000) throw new Error("Choose a workflow file smaller than 250 KB.");
      const result = workflowSchema.parse(JSON.parse(await file.text()));
      setName(result.name);
      setSteps(result.steps.map((step) => ({ ...step, key: nextKey.current++ })));
      setImportError(null);
    } catch (error) {
      setImportError(error instanceof Error ? error : new Error("The workflow JSON could not be read."));
    }
  }

  return <form className="panel edit-form" onSubmit={(event) => { event.preventDefault(); save.mutate(false); }}>
    <SectionTitle title={initial ? "Edit workflow" : "Build a workflow"} detail="Define the actions and the checks that confirm success." />
    <div className="form-grid workflow-top">
      <Field label="Workflow name"><input required maxLength={100} placeholder="Checkout confirmation" value={name} onChange={(event) => setName(event.target.value)} /></Field>
      <div className="actions">
        <button type="button" className="button secondary" onClick={() => input.current?.click()}><Upload size={16} /> Import</button>
        <input className="sr-only" tabIndex={-1} aria-label="Import workflow JSON" ref={input} type="file" accept=".json,application/json" onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void importWorkflow(file);
          event.target.value = "";
        }} />
        <button type="button" className="button secondary" onClick={() => saveFile(new Blob([JSON.stringify(payload(), null, 2)], { type: "application/json" }), "workflow.json")}><Download size={16} /> Export</button>
      </div>
    </div>
    <ErrorNotice error={importError} />
    <div className="step-list">
      {steps.map((step, index) => <StepEditor key={step.key} step={step} index={index} length={steps.length}
        onChange={(next) => setSteps((previous) => previous.map((current) => current.key === step.key ? next : current))}
        onMove={(delta) => move(index, delta)}
        onRemove={() => setSteps((previous) => previous.filter((current) => current.key !== step.key))}
      />)}
      <button type="button" className="button add-step" onClick={() => {
        const key = nextKey.current++;
        setSteps((previous) => [...previous, { key, action: "click" }]);
      }}><Plus size={16} /> Add step</button>
    </div>
    <ErrorNotice error={save.error instanceof Error && save.error.name === "ZodError" ? new Error(workflowSchema.safeParse(payload()).error?.issues[0]?.message ?? "Check the workflow steps.") : save.error} />
    <div className="form-footer"><button type="button" className="button secondary" disabled={save.isPending} onClick={(event) => { if (event.currentTarget.form?.reportValidity()) save.mutate(true); }}>Save and preview</button><button className="button primary" disabled={save.isPending}>{save.isPending ? "Saving…" : initial ? "Save new version" : "Save workflow"}</button></div>
  </form>;
}

export function WorkflowRegistry({ token, workflows, onPreview }: { token: string; workflows: Workflow[]; onPreview: (workflow: Workflow) => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  function choose(id: string | null) {
    setSelected(id);
    setRevision((value) => value + 1);
  }

  return <div className="registry-layout">
    <section className="panel records">
      <SectionTitle title="Workflows" action={<button type="button" className="icon-button" aria-label="New workflow" title="New workflow" onClick={() => choose(null)}><Plus size={18} /></button>} />
      {workflows.length === 0 ? <Empty title="No workflows saved" detail="Create a workflow or load the starter suite from Overview." /> : <div className="record-list">
        {workflows.map((workflow) => <button key={workflow.id} type="button" className={`record ${selected === workflow.id ? "selected" : ""}`} onClick={() => choose(workflow.id)}>
          <span className="record-icon"><Route size={18} /></span><span className="record-text"><strong>{workflow.name}</strong><span>{workflow.steps.length} steps</span></span><span className="version">v{workflow.version}</span>
        </button>)}
      </div>}
    </section>
    <WorkflowEditor key={revision} token={token} initial={workflows.find((workflow) => workflow.id === selected) ?? null} onSaved={(workflow) => choose(workflow.id)} onPreview={onPreview} />
  </div>;
}

export function ModeBadge({ mode }: { mode: Target["mode"] }) {
  return <Badge value={mode === "browser_only" ? "browser-only" : label(mode)} />;
}
