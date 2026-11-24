"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Globe2,
  Plus,
  Route,
} from "lucide-react";
import {
  api,
  arrayPage,
  label,
  type Target,
  type TargetInput,
  type Workflow,
} from "@/lib/api";
import { Badge, Empty, ErrorNotice, Field, Loading, SectionTitle, Select } from "./ui";
import { ListControls, Pagination, useListState } from "./platform-ui";
import { platformKey } from "@/lib/platform";
import { WorkflowEditor } from "./workflow-builder";

const blankTarget: TargetInput = {
  name: "",
  origin: "",
  navigation_origins: [],
  resource_origins: [],
  mode: "browser_only",
  rate_limit: 30,
  session_policy: "fresh",
  authorized: true,
  concurrency: 1,
  acceptance_gate: "verified_clean",
};

function TargetEditor({
  token,
  initial,
  onSaved,
}: {
  token: string;
  initial: Target | null;
  onSaved: (target: Target) => void;
}) {
  const [values, setValues] = useState<TargetInput>(
    initial
      ? {
          name: initial.name,
          origin: initial.origin,
          navigation_origins: initial.navigation_origins ?? [],
          resource_origins: initial.resource_origins ?? [],
          mode: initial.mode,
          rate_limit: initial.rate_limit,
          session_policy: initial.session_policy,
          authorized: true,
          concurrency: initial.concurrency ?? 1,
          acceptance_gate: initial.acceptance_gate ?? "verified_clean",
        }
      : blankTarget,
  );
  const [resourceText, setResourceText] = useState(
    values.resource_origins.join("\n"),
  );
  const [navigationText, setNavigationText] = useState(
    (values.navigation_origins ?? []).join("\n"),
  );
  const query = useQueryClient();
  const save = useMutation({
    mutationFn: () =>
      api<Target>(initial ? `/targets/${initial.id}` : "/targets", token, {
        method: initial ? "PUT" : "POST",
        body: {
          ...values,
          name: values.name.trim(),
          origin: values.origin.trim(),
          resource_origins: resourceText.split(/\s+/).filter(Boolean),
          navigation_origins: navigationText.split(/\s+/).filter(Boolean),
        },
      }),
    onSuccess: (target) => {
      query.setQueryData<Target[]>(["targets", token], (list = []) => [
        ...list.filter((entry) => entry.id !== target.id),
        target,
      ]);
      void query.invalidateQueries({ queryKey: ["targets", token] });
      onSaved(target);
    },
  });

  function set<K extends keyof TargetInput>(key: K, value: TargetInput[K]) {
    setValues((previous) => ({ ...previous, [key]: value }));
  }

  return (
    <form
      className="panel edit-form"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <SectionTitle
        title={initial ? "Edit site" : "Register a site"}
        detail={
          initial
            ? "Saving creates a new version for future runs."
            : "Choose a site you control or have permission to test."
        }
      />
      <div className="form-grid">
        <Field label="Site name">
          <input
            required
            maxLength={100}
            value={values.name}
            placeholder="Checkout staging"
            onChange={(event) => set("name", event.target.value)}
          />
        </Field>
        <Field
          label="Navigation origin"
          hint="Scheme, hostname, and port, with no path."
        >
          <input
            required
            type="url"
            value={values.origin}
            placeholder="https://staging.example.com"
            onChange={(event) => set("origin", event.target.value)}
          />
        </Field>
        <Field
          label="Integration mode"
          hint="Instrumented targets correlate workflow completion with server-verified verdicts."
        >
          <Select
            value={values.mode}
            onChange={(event) =>
              set("mode", event.target.value as TargetInput["mode"])
            }
          >
            <option value="browser_only">Browser-only</option>
            <option value="instrumented">Instrumented</option>
          </Select>
        </Field>
        <Field label="Session policy">
          <Select
            value={values.session_policy}
            onChange={(event) =>
              set(
                "session_policy",
                event.target.value as TargetInput["session_policy"],
              )
            }
          >
            <option value="fresh">Fresh session each trial</option>
            <option value="repeat_within_trial">Repeat within a trial</option>
          </Select>
        </Field>
        <Field
          label="Site concurrency limit"
          hint="Limits simultaneous sessions across batches and schedules."
        >
          <input
            required
            type="number"
            min={1}
            max={500}
            value={values.concurrency ?? 1}
            onChange={(event) => set("concurrency", event.target.valueAsNumber)}
          />
        </Field>
        {values.mode === "instrumented" && (
          <Field label="Benchmark acceptance gate">
            <Select
              value={values.acceptance_gate ?? "verified_clean"}
              onChange={(event) =>
                set(
                  "acceptance_gate",
                  event.target.value as "verified_clean" | "observe_only",
                )
              }
            >
              <option value="verified_clean">
                Require server-verified Clean
              </option>
              <option value="observe_only">
                Observe completion independently
              </option>
            </Select>
          </Field>
        )}
        <Field
          label="Trial starts per minute"
          hint="Instrumented targets also obey the configured AdScore limit, which may be lower."
        >
          <input
            required
            type="number"
            min={1}
            max={6000}
            value={values.rate_limit}
            onChange={(event) => set("rate_limit", event.target.valueAsNumber)}
          />
        </Field>
      </div>
      <Field
        label="Additional navigation origins"
        hint="Enter optional HTTPS origins, one per line. The primary origin is already included. Saving makes these origins available to future runs."
      >
        <textarea
          rows={3}
          spellCheck={false}
          className="mono"
          value={navigationText}
          onChange={(event) => setNavigationText(event.target.value)}
          placeholder="https://checkout.example.com"
        />
      </Field>
      <Field
        label="Resource origins"
        hint="Pages load public HTTPS CDN assets automatically. Add origins for other cross-origin read-only requests, one HTTPS origin per line."
      >
        <textarea
          rows={3}
          spellCheck={false}
          className="mono"
          value={resourceText}
          onChange={(event) => setResourceText(event.target.value)}
        />
      </Field>
      <ErrorNotice error={save.error} />
      <div className="form-footer">
        <button className="button primary" disabled={save.isPending}>
          {save.isPending
            ? "Saving…"
            : initial
              ? "Save new version"
              : "Register site"}
        </button>
      </div>
    </form>
  );
}

export function SiteRegistry({
  token,
  targets,
}: {
  token: string;
  targets: Target[];
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [editorRevision, setEditorRevision] = useState(0);
  const listing = useListState(20);
  const entries = useQuery({ queryKey: platformKey(token, "target-registry", listing.params), queryFn: () => arrayPage<Target>(`/targets?${listing.params}`, token) });
  const records = entries.data?.items ?? [];
  const [cachedTarget, setCachedTarget] = useState<Target | null>(null);
  const editing = records.find((target) => target.id === selected) ?? targets.find((target) => target.id === selected) ?? (cachedTarget?.id === selected ? cachedTarget : null);
  function choose(id: string | null) {
    setSelected(id);
    setEditorRevision((revision) => revision + 1);
  }
  return (
    <div className="registry-layout">
      <section className="panel records">
        <SectionTitle
          title="Registered sites"
          action={
            <button
              type="button"
              className="icon-button"
              aria-label="New site"
              title="New site"
              onClick={() => choose(null)}
            >
              <Plus size={18} />
            </button>
          }
        />
        <ListControls label="sites" state={listing}>
          <Select aria-label="Site integration filter" value={listing.filters.mode ?? ""} onChange={(event) => listing.setFilter("mode", event.target.value)}><option value="">All integrations</option><option value="browser_only">Browser-only</option><option value="instrumented">Instrumented</option></Select>
        </ListControls>
        <ErrorNotice error={entries.error} />
        {entries.isPending ? <Loading /> : records.length === 0 ? (
          <Empty
            title="No sites registered"
            detail="Register a site to make it available to your workflows."
          />
        ) : (
          <div className="record-list">
            {records.map((target) => (
              <button
                key={target.id}
                type="button"
                className={`record ${selected === target.id ? "selected" : ""}`}
                onClick={() => { setCachedTarget(target); choose(target.id); }}
              >
                <span className="record-icon">
                  <Globe2 size={18} />
                </span>
                <span className="record-text">
                  <strong>{target.name}</strong>
                  <span>{target.origin}</span>
                </span>
                <span className="version">v{target.version}</span>
              </button>
            ))}
          </div>
        )}
        <Pagination total={entries.data?.total ?? 0} offset={listing.offset} limit={20} onChange={listing.setOffset} />
      </section>
      <div className="stack">
        <TargetEditor
          key={editorRevision}
          token={token}
          initial={editing}
          onSaved={(site) => { setCachedTarget(site); choose(site.id); void entries.refetch(); }}
        />
      </div>
    </div>
  );
}

export function WorkflowRegistry({
  token,
  workflows,
  onPreview,
}: {
  token: string;
  workflows: Workflow[];
  onPreview: (workflow: Workflow) => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const listing = useListState(20);
  const entries = useQuery({ queryKey: platformKey(token, "workflow-registry", listing.params), queryFn: () => arrayPage<Workflow>(`/workflows?${listing.params}`, token) });
  const records = entries.data?.items ?? [];
  const [cachedWorkflow, setCachedWorkflow] = useState<Workflow | null>(null);
  const editing = cachedWorkflow?.id === selected
    ? cachedWorkflow
    : records.find((workflow) => workflow.id === selected) ??
      workflows.find((workflow) => workflow.id === selected) ??
      null;
  function choose(id: string | null) {
    setSelected(id);
    setRevision((value) => value + 1);
  }

  return (
    <div className="registry-layout">
      <section className="panel records">
        <SectionTitle
          title="Workflows"
          action={
            <button
              type="button"
              className="icon-button"
              aria-label="New workflow"
              title="New workflow"
              onClick={() => choose(null)}
            >
              <Plus size={18} />
            </button>
          }
        />
        <ListControls label="workflows" state={listing} />
        <ErrorNotice error={entries.error} />
        {entries.isPending ? <Loading /> : records.length === 0 ? (
          <Empty
            title="No workflows saved"
            detail="Create a workflow to run on a registered site."
          />
        ) : (
          <div className="record-list">
            {records.map((workflow) => (
              <button
                key={workflow.id}
                type="button"
                className={`record ${selected === workflow.id ? "selected" : ""}`}
                onClick={() => { setCachedWorkflow(workflow); choose(workflow.id); }}
              >
                <span className="record-icon">
                  <Route size={18} />
                </span>
                <span className="record-text">
                  <strong>{workflow.name}</strong>
                  <span>{workflow.steps.length} {workflow.steps.length === 1 ? "step" : "steps"}</span>
                </span>
                <span className="version">v{workflow.version}</span>
              </button>
            ))}
          </div>
        )}
        <Pagination total={entries.data?.total ?? 0} offset={listing.offset} limit={20} onChange={listing.setOffset} />
      </section>
      <WorkflowEditor
        key={revision}
        token={token}
        initial={editing}
        onSaved={(workflow) => { setCachedWorkflow(workflow); choose(workflow.id); void entries.refetch(); }}
        onPreview={onPreview}
      />
    </div>
  );
}

export function ModeBadge({ mode }: { mode: Target["mode"] }) {
  return (
    <Badge value={mode === "browser_only" ? "browser-only" : label(mode)} />
  );
}
