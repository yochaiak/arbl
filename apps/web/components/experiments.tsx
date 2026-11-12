"use client";

import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Copy,
  FlaskConical,
  History,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import { api, dateTime } from "@/lib/api";
import {
  type Adapter,
  type Batch,
  type BatchInput,
  type PageResult,
  type ProxyPolicy,
  type Profile,
  type SharedProps,
  configuredDraft,
  platformKey,
} from "@/lib/platform";
import type { components } from "@/lib/schema";
import {
  Badge,
  Empty,
  ErrorNotice,
  Field,
  Loading,
  SectionTitle,
  Select,
} from "./ui";
import { DetailGrid, ListControls, Modal, Pagination, useListState } from "./platform-ui";
import { BatchFields, defaultBatch } from "./sessions";

type ExperimentInput = Omit<
  components["schemas"]["ExperimentInput"],
  "groups"
> & { groups: BatchInput[]; concurrency: number };
interface Experiment extends ExperimentInput {
  id: string;
  version: number;
  status: string;
  created_at: string;
}
interface Execution {
  id: string;
  version: number;
  created_at: string;
  batches: Batch[];
}
interface Validation {
  status: string;
  issues: Array<{ adapter: string; reason: string }>;
  live_verified: boolean;
}
function ExperimentEditor({
  token,
  targets,
  workflows,
  initial,
  close,
}: SharedProps & { initial?: Experiment; close: () => void }) {
  const [value, setValue] = useState<ExperimentInput>(() =>
    configuredDraft<ExperimentInput>(
      {
        name: "Browser comparison",
        description: "",
        hypothesis: "",
        provider: "adscore",
        baseline_adapter: "playwright",
        tags: [],
        groups: [defaultBatch(targets, workflows)],
        concurrency: 1,
      },
      initial,
    ),
  );
  const [groupIndex, setGroupIndex] = useState(0);
  const query = useQueryClient();
  const adapters = useQuery({
    queryKey: platformKey(token, "adapters"),
    queryFn: () => api<Adapter[]>("/adapters", token),
  });
  const policies = useQuery({
    queryKey: platformKey(token, "proxy-policies"),
    queryFn: () =>
      api<PageResult<ProxyPolicy>>("/proxy-policies?limit=100", token),
  });
  const profiles = useQuery({
    queryKey: platformKey(token, "profile-options"),
    queryFn: () => api<PageResult<Profile>>("/profiles?limit=500", token),
  });
  const save = useMutation({
    mutationFn: () =>
      api(initial ? `/experiments/${initial.id}` : "/experiments", token, {
        method: initial ? "PUT" : "POST",
        body: value,
      }),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "experiments"),
      });
      close();
    },
  });
  const patch = (next: Partial<ExperimentInput>) =>
    setValue({ ...value, ...next });
  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }
  const group = value.groups[groupIndex];
  const total = value.groups.reduce((count, item) => count + item.count, 0);
  return (
    <Modal
      title={
        initial ? "Edit experiment definition" : "Save an experiment definition"
      }
      detail="Declare the comparison once. Every execution keeps an immutable definition snapshot."
      close={close}
      wide
    >
      <form onSubmit={submit}>
        <div className="modal-body stack">
          <div className="form-grid">
            <Field label="Experiment name">
              <input
                required
                maxLength={100}
                value={value.name}
                onChange={(e) => patch({ name: e.target.value })}
              />
            </Field>
            <Field label="Baseline browser">
              <Select
                value={value.baseline_adapter}
                onChange={(e) =>
                  patch({
                    baseline_adapter: e.target
                      .value as ExperimentInput["baseline_adapter"],
                  })
                }
              >
                {adapters.data?.map((adapter) => (
                  <option key={adapter.id} value={adapter.id}>
                    {adapter.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Run concurrency" hint="Maximum simultaneous sessions across all groups. Each group's lower concurrency limit still applies.">
              <input required type="number" min={1} max={500} value={value.concurrency} onChange={(e) => patch({ concurrency: Number(e.target.value) })} />
            </Field>
          </div>
          <Field label="Description">
            <textarea
              rows={2}
              maxLength={4000}
              value={value.description}
              onChange={(e) => patch({ description: e.target.value })}
            />
          </Field>
          <Field label="Experiment hypothesis">
            <textarea
              rows={2}
              maxLength={4000}
              value={value.hypothesis}
              onChange={(e) => patch({ hypothesis: e.target.value })}
              placeholder="State the expected difference and the conditions under which it will be assessed."
            />
          </Field>
          <Field label="Tags" hint="Comma-separated; up to 20 tags.">
            <input
              value={value.tags?.join(", ") ?? ""}
              onChange={(e) =>
                patch({
                  tags: e.target.value
                    .split(",")
                    .map((tag) => tag.trim())
                    .filter(Boolean),
                })
              }
            />
          </Field>
          <div
            className="experiment-groups"
            role="group"
            aria-label="Experiment groups"
          >
            {value.groups.map((item, index) => (
              <button
                key={item.idempotency_key}
                type="button"
                className={`button ${groupIndex === index ? "primary" : "secondary"} small`}
                aria-pressed={groupIndex === index}
                onClick={() => setGroupIndex(index)}
              >
                Group {index + 1} · {item.count}
              </button>
            ))}
            <button
              type="button"
              className="button secondary small"
              disabled={value.groups.length >= 20}
              onClick={() => {
                patch({
                  groups: [
                    ...value.groups,
                    {
                      ...defaultBatch(targets, workflows),
                      name: `Comparison group ${value.groups.length + 1}`,
                      mode: value.groups[0].mode,
                    },
                  ],
                });
                setGroupIndex(value.groups.length);
              }}
            >
              <Plus size={14} />
              Add group
            </button>
          </div>
          <div className="subsection-header">
            <h3>Group {groupIndex + 1}: target, workflow, and conditions</h3>
            {value.groups.length > 1 && (
              <button
                type="button"
                className="button danger small"
                onClick={() => {
                  patch({
                    groups: value.groups.filter(
                      (_, index) => index !== groupIndex,
                    ),
                  });
                  setGroupIndex(0);
                }}
              >
                <Trash2 size={14} />
                Remove group
              </button>
            )}
          </div>
          <BatchFields
            token={token}
            value={group}
            onChange={(next) =>
              patch({
                groups: value.groups.map((item, index) =>
                  index === groupIndex ? next : { ...item, mode: next.mode },
                ),
              })
            }
            targets={targets}
            workflows={workflows}
            adapters={adapters.data ?? []}
            policies={policies.data?.items ?? []}
            profiles={profiles.data?.items ?? []}
          />
          <div className="notice">
            <FlaskConical size={17} />
            {value.groups.length} matched condition groups ·{" "}
            {total.toLocaleString()} planned logical sessions. Evidence mode is
            shared across groups. Saving does not launch browsers.
          </div>
          <ErrorNotice
            error={
              save.error || adapters.error || policies.error || profiles.error
            }
          />
        </div>
        <div className="modal-footer">
          <button type="button" className="button secondary" onClick={close}>
            Cancel
          </button>
          <button
            className="button primary"
            disabled={
              save.isPending ||
              value.groups.some(
                (item) =>
                  !item.target_id || !item.workflow_id || !item.adapters.length,
              )
            }
          >
            {save.isPending
              ? "Saving…"
              : initial
                ? "Save experiment version"
                : "Save draft experiment"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
function ExperimentHistory({
  token,
  experiment,
  close,
  onCompare,
  onCompareRun,
}: {
  token: string;
  experiment: Experiment;
  close: () => void;
  onCompare: (id: string) => void;
  onCompareRun: (experimentId: string, runId: string) => void;
}) {
  const runs = useQuery({
    queryKey: platformKey(token, "experiment-runs", experiment.id),
    queryFn: () =>
      api<Execution[]>(`/experiments/${experiment.id}/runs`, token),
    refetchInterval: 5000,
  });
  return (
    <Modal
      title={`${experiment.name} · executions`}
      detail="Execution history preserves the definition version and each group's independent evidence."
      close={close}
      wide
    >
      <div className="modal-body stack">
        <DetailGrid
          items={[
            ["Current definition", `v${experiment.version}`],
            ["Baseline", experiment.baseline_adapter],
            ["Condition groups", experiment.groups.length],
            ["Run concurrency", experiment.concurrency ?? 1],
            ["Status", <Badge key="status" value={experiment.status} />],
          ]}
        />
        <ErrorNotice error={runs.error} />
        {runs.isPending ? (
          <Loading />
        ) : runs.data?.length ? (
          <div className="stack">
            {runs.data.map((run) => (
              <section key={run.id} className="experiment-execution">
                <div className="subsection-header">
                  <strong>
                    Execution {run.id.slice(0, 8)} · definition v{run.version}
                  </strong>
                  <small className="muted">{dateTime(run.created_at)}</small>
                </div>
                <button className="button primary small" onClick={() => onCompareRun(experiment.id, run.id)}>View execution report</button>
                {run.batches.map((batch) => (
                  <div className="experiment-batch" key={batch.id}>
                    <span>
                      <strong>{batch.name}</strong>
                      <small>{batch.count} sessions</small>
                    </span>
                    <Badge value={batch.mode ?? "unknown"} />
                    <Badge value={batch.status} />
                    <button
                      className="button secondary small"
                      onClick={() => onCompare(batch.id)}
                    >
                      View comparison
                    </button>
                  </div>
                ))}
              </section>
            ))}
          </div>
        ) : (
          <Empty
            title="No executions yet"
            detail="Validate this saved definition, then launch its condition groups."
          />
        )}
      </div>
    </Modal>
  );
}
export function Experiments(
  props: SharedProps & { onCompare: (id: string) => void; onCompareRun: (experimentId: string, runId: string) => void },
) {
  const { token, onCompare, onCompareRun } = props;
  const [editing, setEditing] = useState<Experiment | null | undefined>(
    undefined,
  );
  const [history, setHistory] = useState<Experiment | null>(null);
  const listing = useListState();
  const { offset, setOffset } = listing;
  const [validation, setValidation] = useState<{
    experiment: Experiment;
    result: Validation;
  } | null>(null);
  const [launched, setLaunched] = useState<string | null>(null);
  const query = useQueryClient();
  const entries = useQuery({
    queryKey: platformKey(token, "experiments", listing.params),
    queryFn: () =>
      api<PageResult<Experiment>>(
        `/experiments?${listing.params}`,
        token,
      ),
  });
  const validate = useMutation({
    mutationFn: (experiment: Experiment) =>
      api<Validation>(`/experiments/${experiment.id}/validate`, token, {
        method: "POST",
      }),
    onSuccess: (result, experiment) => {
      setValidation({ experiment, result });
      void query.invalidateQueries({
        queryKey: platformKey(token, "experiments"),
      });
    },
  });
  const clone = useMutation({
    mutationFn: (id: string) =>
      api(`/experiments/${id}/clone`, token, { method: "POST" }),
    onSuccess: () =>
      void query.invalidateQueries({
        queryKey: platformKey(token, "experiments"),
      }),
  });
  const execute = useMutation({
    mutationFn: ({ id, key }: { id: string; key: string }) =>
      api<{ id: string; batch_ids: string[] }>(
        `/experiments/${id}/runs`,
        token,
        { method: "POST", headers: { "Idempotency-Key": key } },
      ),
    onSuccess: (result) => {
      setLaunched(result.id);
      void query.invalidateQueries({ queryKey: ["platform", token] });
    },
  });
  const [executionKeys, setExecutionKeys] = useState<Record<string, string>>(
    {},
  );
  function launch(id: string) {
    const key = executionKeys[id] ?? crypto.randomUUID();
    setExecutionKeys({ ...executionKeys, [id]: key });
    execute.mutate(
      { id, key },
      {
        onSuccess: () =>
          setExecutionKeys((current) => {
            const next = { ...current };
            delete next[id];
            return next;
          }),
      },
    );
  }
  return (
    <div className="stack">
      <div className="page-toolbar">
        <div className="context-note">
          <FlaskConical size={17} />
          Saved definitions, separately validated executions.
        </div>
        <button className="button primary" onClick={() => setEditing(null)}>
          <Plus size={16} />
          New experiment
        </button>
      </div>
      <ErrorNotice
        error={entries.error || validate.error || clone.error || execute.error}
      />
      {launched && (
        <div className="notice" role="status">
          <CheckCircle2 size={17} />
          Experiment execution {launched.slice(0, 12)} was created. Open history
          to inspect its group batches.
        </div>
      )}
      <section className="panel data-panel">
        <SectionTitle
          title="Experiment definitions"
          detail="Versioned, reusable comparison plans across target and workflow groups."
        />
        <ListControls label="experiments" state={listing}>
          <Select aria-label="Experiment state" value={listing.filters.status ?? ""} onChange={(event) => listing.setFilter("status", event.target.value)}><option value="">All states</option><option value="draft">Draft</option><option value="ready">Ready</option><option value="blocked">Blocked</option></Select>
          <Select aria-label="Experiment evidence" value={listing.filters.mode ?? ""} onChange={(event) => listing.setFilter("mode", event.target.value)}><option value="">All evidence</option><option value="live">Live</option><option value="demo">Demo</option></Select>
        </ListControls>
        {entries.isPending ? (
          <Loading />
        ) : entries.data?.items.length ? (
          <>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Experiment</th>
                    <th>Version</th>
                    <th>Evidence</th>
                    <th>Groups / planned</th>
                    <th>Run concurrency</th>
                    <th>Readiness</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.data.items.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <button
                          className="text-button"
                          onClick={() => setEditing(item)}
                        >
                          <strong>{item.name}</strong>
                        </button>
                        <small className="table-subtitle">
                          {item.description ||
                            `Baseline: ${item.baseline_adapter}`}
                        </small>
                      </td>
                      <td className="mono">v{item.version}</td>
                      <td>
                        <Badge value={item.groups[0].mode} />
                      </td>
                      <td>
                        {item.groups.length} /{" "}
                        {item.groups
                          .reduce((n, g) => n + g.count, 0)
                          .toLocaleString()}
                      </td>
                      <td>{item.concurrency ?? 1}</td>
                      <td>
                        <Badge value={item.status} />
                      </td>
                      <td>
                        <div className="actions">
                          <button
                            className="button secondary small"
                            disabled={validate.isPending}
                            onClick={() => validate.mutate(item)}
                          >
                            Validate
                          </button>
                          <button
                            className="icon-button"
                            aria-label={`Execute ${item.name}`}
                            disabled={
                              item.status !== "ready" || execute.isPending
                            }
                            onClick={() => launch(item.id)}
                          >
                            <Play size={15} />
                          </button>
                          <button
                            className="icon-button"
                            aria-label={`Clone ${item.name}`}
                            disabled={clone.isPending}
                            onClick={() => clone.mutate(item.id)}
                          >
                            <Copy size={15} />
                          </button>
                          <button
                            className="icon-button"
                            aria-label={`History for ${item.name}`}
                            onClick={() => setHistory(item)}
                          >
                            <History size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              total={entries.data.total}
              offset={offset}
              limit={50}
              onChange={setOffset}
            />
          </>
        ) : (
          <Empty
            title="Design a reproducible comparison"
            detail="Save a hypothesis, baseline browser, and multiple target/workflow groups. Validate before launching an immutable execution."
            action={
              <button
                className="button secondary"
                onClick={() => setEditing(null)}
              >
                <Plus size={16} />
                Save first experiment
              </button>
            }
          />
        )}
      </section>
      {editing !== undefined && (
        <ExperimentEditor
          {...props}
          initial={editing ?? undefined}
          close={() => setEditing(undefined)}
        />
      )}{" "}
      {history && (
        <ExperimentHistory
          token={token}
          experiment={history}
          close={() => setHistory(null)}
          onCompare={(id) => {
            setHistory(null);
            onCompare(id);
          }}
          onCompareRun={(experimentId, runId) => {
            setHistory(null);
            onCompareRun(experimentId, runId);
          }}
        />
      )}{" "}
      {validation && (
        <Modal
          title={`Readiness · ${validation.experiment.name}`}
          close={() => setValidation(null)}
        >
          <div className="modal-body stack">
            <Badge value={validation.result.status} />
            <p className="notice">
              Configuration readiness is distinct from a live verification
              result. This validation does not launch browsers or verify an
              AdScore verdict.
            </p>
            {validation.result.issues.length ? (
              <ul className="method-list">
                {validation.result.issues.map((issue, index) => (
                  <li key={index}>
                    <strong>{issue.adapter}</strong>: {issue.reason}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">
                No configuration blockers were reported for this evidence mode.
              </p>
            )}
          </div>
          <div className="modal-footer">
            <button
              className="button secondary"
              onClick={() => setValidation(null)}
            >
              Close
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
