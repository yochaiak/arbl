"use client";

import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowRight,
  Check,
  ChevronRight,
  Clock3,
  Download,
  CopyPlus,
  Layers3,
  Monitor,
  MousePointer2,
  Pause,
  Play,
  Plus,
  Search,
  ShieldCheck,
  Square,
  X,
} from "lucide-react";
import {
  api,
  dateTime,
  label,
  ms,
  request,
  saveFile,
  type Target,
  type Workflow,
} from "@/lib/api";
import {
  type Adapter,
  type Batch,
  type BatchItem,
  type BatchInput,
  type BrowserSession,
  type PageResult,
  type Profile,
  type ProxyPolicy,
  platformKey,
  sessionActive,
  humanValue,
} from "@/lib/platform";
import {
  Badge,
  Empty,
  ErrorNotice,
  Field,
  Loading,
  SectionTitle,
  Select,
} from "./ui";
import { DetailGrid, ListControls, Modal, Pagination, Segments, useListState } from "./platform-ui";
import { SessionTemplates } from "./session-templates";

export function defaultBatch(
  targets: Target[],
  workflows: Workflow[],
): BatchInput {
  return {
    name: "Workflow batch",
    target_id: targets[0]?.id ?? "",
    workflow_id: workflows[0]?.id ?? "",
    count: 30,
    concurrency: 1,
    adapters: ["playwright"],
    persistence: "fresh",
    preparation: {
      mode: "none",
      min_seconds: 30,
      max_seconds: 90,
      paths: ["/"],
      idle_timeout_seconds: 900,
      lifetime_seconds: 3600,
    },
    proxy_policy_id: null,
    mode: "demo",
    seed: 42,
    artifact_policy: "none",
    artifact_retention_days: 30,
    phase: "pilot",
    hypothesis: "",
    reliability_target: 0.95,
    timeout_seconds: 600,
    request_budget: 1000,
    traffic_budget_bytes: 104857600,
    idempotency_key: crypto.randomUUID(),
  };
}
export function BatchFields({
  token,
  value,
  onChange,
  targets,
  workflows,
  adapters,
  policies,
  profiles = [],
}: {
  token: string;
  value: BatchInput;
  onChange: (value: BatchInput) => void;
  targets: Target[];
  workflows: Workflow[];
  adapters: Adapter[];
  policies: ProxyPolicy[];
  profiles?: Profile[];
}) {
  const patch = (update: Partial<BatchInput>) =>
    onChange({ ...value, ...update });
  return (
    <div className="stack compact-stack">
      <SessionTemplates token={token} value={value} onChange={onChange} />
      <fieldset className="batch-configuration" disabled={Boolean(value.session_template_id)}>
      <div className="form-grid">
        <Field label="Batch name">
          <input
            required
            maxLength={100}
            value={value.name}
            onChange={(e) => patch({ name: e.target.value })}
          />
        </Field>
        <Field label="Evidence source">
          <Select
            value={value.mode}
            onChange={(e) =>
              patch({ mode: e.target.value as BatchInput["mode"] })
            }
          >
            <option value="demo">Demonstration · simulated</option>
            <option value="live">Live execution</option>
          </Select>
        </Field>
        <Field label="Authorized site">
          <Select
            required
            value={value.target_id}
            onChange={(e) => patch({ target_id: e.target.value })}
          >
            <option value="" disabled>
              Select a site
            </option>
            {targets.map((site) => (
              <option key={site.id} value={site.id}>
                {site.name} · v{site.version}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Workflow">
          <Select
            required
            value={value.workflow_id}
            onChange={(e) => patch({ workflow_id: e.target.value })}
          >
            <option value="" disabled>
              Select a workflow
            </option>
            {workflows.map((workflow) => (
              <option key={workflow.id} value={workflow.id}>
                {workflow.name} · v{workflow.version}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <fieldset className="choice-fieldset">
        <legend>Browser distribution</legend>
        <p>Sessions are assigned across the selected browser adapters.</p>
        <div className="adapter-options">
          {(adapters.length
            ? adapters
            : [
                {
                  id: "playwright" as const,
                  reason: "Runtime inventory unavailable",
                  name: "Playwright Chromium",
                  available: false,
                  status: "unknown",
                  capabilities: [],
                },
              ]
          ).map((adapter) => (
            <label
              key={adapter.id}
              className={`adapter-option ${value.adapters.includes(adapter.id) ? "selected" : ""}`}
            >
              <input
                type="checkbox"
                checked={value.adapters.includes(adapter.id)}
                onChange={(e) =>
                  (() => {
                    const adapters = e.target.checked
                      ? [...value.adapters, adapter.id]
                      : value.adapters.filter((id) => id !== adapter.id);
                    const perGroup = value.phase === "confirmation" ? 300 : 30;
                    patch({
                      adapters,
                      count:
                        value.count === perGroup * value.adapters.length
                          ? Math.max(1, adapters.length) * perGroup
                          : value.count,
                      ...(adapter.id === "manual_chrome" && e.target.checked
                        ? {
                            preparation: {
                              ...value.preparation,
                              mode: "manual",
                            },
                          }
                        : {}),
                    });
                  })()
                }
              />
              <span className="adapter-icon">
                <Monitor size={19} />
              </span>
              <span>
                <strong>{adapter.name}</strong>
                <small>
                  {adapter.available
                    ? "Runtime available"
                    : adapter.reason || label(adapter.status)}
                </small>
              </span>
              <Badge value={adapter.status} />
            </label>
          ))}
        </div>
      </fieldset>
      <div className="form-grid">
        <Field
          label="Independent sessions"
          hint="Total across selected browser groups. Maximum 2,000 sessions; up to 500 active."
        >
          <input
            type="number"
            required
            min={1}
            max={2000}
            value={value.count}
            onChange={(e) => patch({ count: Number(e.target.value) })}
          />
        </Field>
        <Field
          label="Maximum concurrency"
          hint="Admission also enforces project, site, proxy, and license limits."
        >
          <input
            type="number"
            required
            min={1}
            max={Math.min(value.count, 500)}
            value={value.concurrency}
            onChange={(e) => patch({ concurrency: Number(e.target.value) })}
          />
        </Field>
        <Field label="Profile persistence">
          <Select
            value={value.persistence}
            onChange={(e) =>
              patch({
                persistence: e.target.value as BatchInput["persistence"],
                profile_ids: [],
              })
            }
          >
            <option value="fresh">Fresh · discard browser state</option>
            <option value="persistent">
              Persistent · retain after clean shutdown
            </option>
          </Select>
        </Field>
        <Field label="Proxy policy">
          <Select
            value={value.proxy_policy_id ?? ""}
            onChange={(e) => patch({ proxy_policy_id: e.target.value || null })}
          >
            <option value="">Direct connectivity</option>
            {policies.map((policy) => (
              <option value={policy.id} key={policy.id}>
                {policy.name} · v{policy.version}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      {value.persistence === "persistent" && profiles.length > 0 && (
        <Field
          label="Reuse existing profiles (optional)"
          hint="Select one available profile per requested session. Leave empty to create independent new profiles."
        >
          <select
            multiple
            className="profile-picker"
            value={value.profile_ids ?? []}
            onChange={(e) =>
              patch({
                profile_ids: Array.from(e.target.selectedOptions).map(
                  (option) => option.value,
                ),
              })
            }
          >
            {profiles.map((profile) => (
              <option
                key={profile.id}
                value={profile.id}
                disabled={profile.status !== "available"}
              >
                {profile.name} · {profile.adapter} · {profile.status}
              </option>
            ))}
          </select>
          {(value.profile_ids?.length ?? 0) > 0 && (
            <button
              type="button"
              className="text-button"
              onClick={() => patch({ profile_ids: [] })}
            >
              Clear selection · create new profiles
            </button>
          )}
        </Field>
      )}
      <fieldset className="choice-fieldset">
        <legend>Preparation</legend>
        <Segments
          label="Preparation mode"
          value={value.preparation.mode}
          onChange={(mode) =>
            patch({ preparation: { ...value.preparation, mode } })
          }
          options={[
            { value: "none", label: "None" },
            { value: "automated", label: "Automated preparation" },
            { value: "manual", label: "Manual control" },
          ]}
        />
        {value.preparation.mode === "automated" && (
          <>
            <p>
              Seeded, bounded activity on approved pages. Recorded as automated
              activity and measured separately from the workflow.
            </p>
            <div className="form-grid">
              <Field label="Minimum seconds">
                <input
                  type="number"
                  min={1}
                  max={300}
                  required
                  value={value.preparation.min_seconds}
                  onChange={(e) =>
                    patch({
                      preparation: {
                        ...value.preparation,
                        min_seconds: Number(e.target.value),
                      },
                    })
                  }
                />
              </Field>
              <Field label="Maximum seconds">
                <input
                  type="number"
                  min={value.preparation.min_seconds}
                  max={300}
                  required
                  value={value.preparation.max_seconds}
                  onChange={(e) =>
                    patch({
                      preparation: {
                        ...value.preparation,
                        max_seconds: Number(e.target.value),
                      },
                    })
                  }
                />
              </Field>
              <Field
                label="Approved paths"
                hint="Comma-separated, site-relative paths."
              >
                <input
                  required
                  value={value.preparation.paths.join(", ")}
                  onChange={(e) =>
                    patch({
                      preparation: {
                        ...value.preparation,
                        paths: e.target.value
                          .split(",")
                          .map((path) => path.trim()),
                      },
                    })
                  }
                />
              </Field>
            </div>
          </>
        )}
        {(value.preparation.mode === "manual" || value.adapters.includes("manual_chrome")) && (
          <div className="stack compact-stack">
            <p className="muted small-text">
              A person controls the browser before explicitly handing back to
              automation. The activity is attributed to a human operator.
            </p>
            <Field
              label="Manual idle timeout (seconds)"
              hint="Defaults to 15 minutes. Session lifetime is configured below."
            >
              <input
                required
                type="number"
                min={30}
                max={3600}
                value={value.preparation.idle_timeout_seconds}
                onChange={(e) =>
                  patch({
                    preparation: {
                      ...value.preparation,
                      idle_timeout_seconds: Number(e.target.value),
                    },
                  })
                }
              />
            </Field>
            <Field
              label="Manual session lifetime (seconds)"
              hint="Defaults to one hour. Automation resumes only after explicit handoff; reaching this limit ends the session."
            >
              <input
                required
                type="number"
                min={value.preparation.idle_timeout_seconds}
                max={7200}
                value={value.preparation.lifetime_seconds ?? 3600}
                onChange={(e) =>
                  patch({
                    preparation: {
                      ...value.preparation,
                      lifetime_seconds: Number(e.target.value),
                    },
                  })
                }
              />
            </Field>
          </div>
        )}
      </fieldset>
      <fieldset className="choice-fieldset">
        <legend>Research declaration</legend>
        <div className="form-grid">
          <Field label="Research phase">
            <Select
              value={value.phase ?? "pilot"}
              onChange={(e) =>
                patch({
                  phase: e.target.value as "pilot" | "confirmation",
                  count:
                    (e.target.value === "confirmation" ? 300 : 30) *
                    value.adapters.length,
                })
              }
            >
              <option value="pilot">Pilot · 30 per browser group</option>
              <option value="confirmation">
                Confirmation · 300 per browser group
              </option>
            </Select>
          </Field>
          <Field label="Reliability threshold (%)">
            <input
              type="number"
              min={1}
              max={100}
              step={0.1}
              value={(value.reliability_target ?? 0.95) * 100}
              onChange={(e) =>
                patch({ reliability_target: Number(e.target.value) / 100 })
              }
            />
          </Field>
          <Field label="Maximum duration per session (seconds)">
            <input
              type="number"
              min={10}
              max={7200}
              value={value.timeout_seconds ?? 600}
              onChange={(e) =>
                patch({ timeout_seconds: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="HTTP request budget per session" hint="Includes preparation and workflow requests. Each phase is recorded separately.">
            <input type="number" required min={1} max={100000} value={value.request_budget ?? 1000} onChange={(event) => patch({ request_budget: Number(event.target.value) })} />
          </Field>
          <Field label="Transport budget per session (MiB)" hint="Measured browser payload and vendor control traffic. This is an estimate, not the provider billing total.">
            <input type="number" required min={0.0009765625} max={2048} step="any" value={(value.traffic_budget_bytes ?? 104857600) / 1048576} onChange={(event) => patch({ traffic_budget_bytes: Math.round(Number(event.target.value) * 1048576) })} />
          </Field>
          <Field label="Known cost per session (optional)">
            <input
              type="number"
              min={0}
              step="0.0001"
              placeholder="Unknown"
              value={value.cost_per_session ?? ""}
              onChange={(e) =>
                patch({
                  cost_per_session:
                    e.target.value === "" ? null : Number(e.target.value),
                })
              }
            />
          </Field>
        </div>
        <Field label="Declared hypothesis (optional)">
          <textarea
            rows={2}
            maxLength={2000}
            value={value.hypothesis ?? ""}
            onChange={(e) => patch({ hypothesis: e.target.value })}
            placeholder="What comparison is this experiment designed to answer?"
          />
        </Field>
      </fieldset>
      <div className="form-grid">
        <Field label="Artifact capture">
          <Select
            value={value.artifact_policy ?? "none"}
            onChange={(e) =>
              patch({
                artifact_policy: e.target
                  .value as BatchInput["artifact_policy"],
              })
            }
          >
            <option value="none">No artifacts</option>
            <option value="on_failure">Capture on failure</option>
            <option value="all">Capture all sessions</option>
          </Select>
        </Field>
        <Field label="Artifact retention (days)">
          <input
            type="number"
            required
            min={1}
            max={365}
            value={value.artifact_retention_days ?? 30}
            onChange={(e) =>
              patch({ artifact_retention_days: Number(e.target.value) })
            }
          />
        </Field>
        <Field
          label="Cost currency (optional)"
          hint="Three-letter currency code. Missing cost inputs remain unknown."
        >
          <input
            maxLength={3}
            pattern="[A-Z]{3}"
            placeholder="USD"
            value={value.cost_currency ?? ""}
            onChange={(e) =>
              patch({ cost_currency: e.target.value.toUpperCase() || null })
            }
          />
        </Field>
      </div>
      <Field
        label="Random seed"
        hint="Saved with the batch to reproduce adapter order and automated preparation."
      >
        <input
          className="short-input"
          type="number"
          min={0}
          required
          value={value.seed}
          onChange={(e) => patch({ seed: Number(e.target.value) })}
        />
      </Field>
      </fieldset>
    </div>
  );
}
export function BatchWizard({
  token,
  targets,
  workflows,
  close,
  onCreated,
}: {
  token: string;
  targets: Target[];
  workflows: Workflow[];
  close: () => void;
  onCreated: () => void;
}) {
  const query = useQueryClient();
  const [value, setValue] = useState(() => defaultBatch(targets, workflows));
  const [stage, setStage] = useState(0);
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
  const create = useMutation({
    mutationFn: () =>
      api<Batch>("/batches", token, { method: "POST", body: value }),
    onSuccess: () => {
      void query.invalidateQueries({ queryKey: ["platform", token] });
      onCreated();
      close();
    },
  });
  const valid =
    value.adapters.length > 0 &&
    value.count >= 1 &&
    value.concurrency >= 1 &&
    value.concurrency <= value.count &&
    (!value.profile_ids?.length || value.profile_ids.length === value.count) &&
    (!value.adapters.includes("manual_chrome") ||
      value.preparation.mode === "manual");
  function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    if (stage === 0) setStage(1);
    else create.mutate();
  }
  return (
    <Modal
      title="Create session batch"
      detail="Independent browser sessions. One repeatable workflow."
      close={close}
      wide
    >
      <form onSubmit={submit}>
        <div className="wizard-steps">
          <span className={stage === 0 ? "current" : ""}>
            <b>1</b> Configure
          </span>
          <ChevronRight size={16} />
          <span className={stage === 1 ? "current" : ""}>
            <b>2</b> Review & launch
          </span>
        </div>
        <div className="modal-body">
          {stage === 0 ? (
            <BatchFields
              token={token}
              value={value}
              onChange={setValue}
              targets={targets}
              workflows={workflows}
              adapters={adapters.data ?? []}
              policies={policies.data?.items ?? []}
              profiles={profiles.data?.items ?? []}
            />
          ) : (
            <div className="stack">
              <div className="review-banner">
                <Layers3 size={28} />
                <div>
                  <h3>{value.count} independent sessions</h3>
                  <p>
                    Up to {value.concurrency} running together, subject to
                    admission capacity.
                  </p>
                </div>
                <Badge value={value.mode} />
              </div>
              <DetailGrid
                items={[
                  ["Site", targets.find((t) => t.id === value.target_id)?.name],
                  [
                    "Workflow",
                    workflows.find((w) => w.id === value.workflow_id)?.name,
                  ],
                  ["Browsers", value.adapters.join(", ")],
                  ["Profile", label(value.persistence)],
                  ["Preparation", label(value.preparation.mode)],
                  ["Random seed", value.seed],
                  ["HTTP request budget", value.request_budget ?? 1000],
                  ["Transport budget", `${(value.traffic_budget_bytes ?? 104857600) / 1048576} MiB per session`],
                  ["Session template", value.session_template_id ? `${value.session_template_id.slice(0, 12)} · v${value.session_template_version}` : "Custom configuration"],
                ]}
              />
              {value.mode === "demo" ? (
                <div className="notice">
                  Demonstration outcomes are simulated and cannot qualify a live
                  recommendation.
                </div>
              ) : (
                <div className="notice">
                  Live execution uses the saved target and workflow versions.
                  Instrumented Clean results require successful server
                  verification.
                </div>
              )}
              <div className="notice">
                <ShieldCheck size={18} /> Persistent profiles have one writer.
                Failed or uncertain saves remain quarantined until reconciled.
              </div>
            </div>
          )}
          <ErrorNotice
            error={create.error || adapters.error || policies.error}
          />
        </div>
        <div className="modal-footer">
          <button
            type="button"
            className="button secondary"
            onClick={stage === 0 ? close : () => setStage(0)}
          >
            {stage === 0 ? "Cancel" : "Back"}
          </button>
          <button
            className="button primary"
            disabled={
              !valid ||
              create.isPending ||
              !value.target_id ||
              !value.workflow_id
            }
          >
            {create.isPending
              ? "Creating…"
              : stage === 0
                ? "Review batch"
                : "Create batch"}
            {stage === 0 ? <ArrowRight size={16} /> : <Play size={16} />}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function SessionViewer({
  token,
  session,
}: {
  token: string;
  session: BrowserSession;
}) {
  const [text, setText] = useState("");
  const [owned, setOwned] = useState(false);
  const query = useQueryClient();
  const command = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api(`/sessions/${session.id}/commands`, token, {
        method: "POST",
        body: { ...body, idempotency_key: crypto.randomUUID() },
      }),
    onSuccess: (_data, body) => {
      if (body.command === "claim_control") setOwned(true);
      if (["release_control", "continue"].includes(String(body.command)))
        setOwned(false);
      void query.invalidateQueries({
        queryKey: platformKey(token, "sessions"),
      });
    },
  });
  const frame = useQuery({
    queryKey: platformKey(token, "frame", session.id),
    queryFn: async () => {
      const response = await request(`/sessions/${session.id}/frame`, token);
      const blob = await response.blob();
      return await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
    },
    enabled: sessionActive(session.status),
    refetchInterval: owned ? 1000 : 5000,
    retry: false,
  });
  return (
    <section className="viewer">
      <div className="viewer-toolbar">
        <span>
          <MousePointer2 size={16} />
          {owned ? "You control this browser" : "Manual browser viewer"}
        </span>
        <button
          className="button secondary small"
          disabled={command.isPending}
          onClick={() =>
            command.mutate({
              command: owned ? "release_control" : "claim_control",
            })
          }
        >
          {owned ? "Release control" : "Take control"}
        </button>
      </div>
      <div className={`viewer-frame ${owned ? "interactive" : ""}`}>
        {/* The frame is authenticated binary data, never a public browser endpoint. */}
        {frame.data ? (
          // Authenticated transient frames must bypass public image optimization.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={frame.data}
            alt="Live browser session"
            onClick={(e) => {
              if (!owned) return;
              const rect = e.currentTarget.getBoundingClientRect();
              command.mutate({
                command: "click",
                x: Math.round(
                  ((e.clientX - rect.left) * e.currentTarget.naturalWidth) /
                    rect.width,
                ),
                y: Math.round(
                  ((e.clientY - rect.top) * e.currentTarget.naturalHeight) /
                    rect.height,
                ),
              });
            }}
          />
        ) : (
          <Empty
            title="Waiting for a browser frame"
            detail="The viewer becomes available when the session enters manual preparation."
          />
        )}
      </div>
      {owned && (
        <>
          <div className="viewer-input">
            <input
              aria-label="Text to enter in browser"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Type into the focused browser field"
            />
            <button
              className="button secondary small"
              disabled={!text || command.isPending}
              onClick={() => command.mutate({ command: "type", text })}
            >
              Type
            </button>
            <button
              className="button secondary small"
              onClick={() => command.mutate({ command: "key", key: "Enter" })}
            >
              Enter
            </button>
          </div>
          <div className="actions">
            <button
              className="button secondary small"
              onClick={() =>
                command.mutate({ command: "scroll", delta_y: 480 })
              }
            >
              Scroll down
            </button>
            <button
              className="button secondary small"
              onClick={() =>
                command.mutate({ command: "scroll", delta_y: -480 })
              }
            >
              Scroll up
            </button>
            <button
              className="button primary small"
              disabled={command.isPending}
              onClick={() => command.mutate({ command: "continue" })}
            >
              <Play size={14} />
              Hand back to workflow
            </button>
          </div>
        </>
      )}
      <ErrorNotice error={command.error} />
      {frame.error && (
        <p className="muted small-text">
          Frame unavailable:{" "}
          {frame.error instanceof Error
            ? frame.error.message
            : "No frame returned."}
        </p>
      )}
    </section>
  );
}
function SessionDetail({
  token,
  session,
  close,
}: {
  token: string;
  session: BrowserSession;
  close: () => void;
}) {
  const query = useQueryClient();
  const requestUsage = session.metadata.request_usage as { total: number; budget: number; by_phase: Record<string, number>; exhausted: boolean } | undefined;
  const transportUsage = session.metadata.transport_usage as { total_bytes: number; budget_consumed_bytes: number; limit_bytes: number; budget_exhausted: boolean } | undefined;
  const [viewer, setViewer] = useState(false);
  const stop = useMutation({
    mutationFn: () =>
      api(`/sessions/${session.id}/commands`, token, {
        method: "POST",
        body: { command: "stop", idempotency_key: crypto.randomUUID() },
      }),
    onSuccess: () =>
      void query.invalidateQueries({
        queryKey: platformKey(token, "sessions"),
      }),
  });
  const events = useQuery({
    queryKey: platformKey(token, "session-events", session.id),
    queryFn: () =>
      api<
        PageResult<{
          id: string;
          sequence: number;
          kind: string;
          created_at: string;
          detail?: Record<string, unknown>;
        }>
      >(`/sessions/${session.id}/events`, token),
    refetchInterval: sessionActive(session.status) ? 5000 : false,
  });
  const artifacts = useQuery({
    queryKey: platformKey(token, "artifacts", session.id),
    queryFn: () =>
      api<
        Array<{
          id: string;
          name: string;
          media_type: string;
          size: number;
          checksum: string;
          expires_at: string;
        }>
      >(`/sessions/${session.id}/artifacts`, token),
    refetchInterval: sessionActive(session.status) ? 5000 : false,
  });
  const download = useMutation({
    mutationFn: async ({ id, name }: { id: string; name: string }) => {
      const response = await request(`/artifacts/${id}`, token);
      saveFile(await response.blob(), name);
    },
  });
  const observations = useQuery({
    queryKey: platformKey(token, "observations", session.id),
    queryFn: () =>
      api<
        Array<{
          id: string;
          verification_status: string;
          verdict: string | null;
          raw_code: number | null;
          accepted: boolean;
          source: string;
          provider_version: string | null;
          created_at: string;
        }>
      >(`/sessions/${session.id}/observations`, token),
    refetchInterval: sessionActive(session.status) ? 5000 : false,
  });
  return (
    <aside className="detail-drawer" aria-label="Session details">
      <div className="drawer-heading">
        <div>
          <span className="eyebrow">SESSION</span>
          <h2 className="mono">{session.id.slice(0, 12)}</h2>
        </div>
        <button
          className="icon-button"
          onClick={close}
          aria-label="Close session details"
        >
          <X size={20} />
        </button>
      </div>
      <div className="drawer-body stack">
        <div className="actions">
          <Badge value={session.status} />
          <Badge value={session.mode} />
        </div>
        <DetailGrid
          items={[
            ["Browser", session.adapter],
            ["Profile", label(session.persistence)],
            [
              "Operational outcome",
              session.outcome ? label(session.outcome) : "Pending",
            ],
            [
              "Workflow outcome",
              session.workflow_outcome
                ? label(session.workflow_outcome)
                : "Unrecorded",
            ],
            ["Duration", ms(session.elapsed_ms)],
            ["Ground truth", label(session.ground_truth ?? "unrecorded")],
            ["Created", dateTime(session.created_at)],
            [
              "Environment epoch",
              session.epoch ?? session.environment_epoch ?? 1,
            ],
          ]}
        />
        {session.error && <ErrorNotice error={session.error} />}
        {(requestUsage || transportUsage) && <section>
          <h3>Execution budgets</h3>
          <DetailGrid items={[
            ["HTTP requests", requestUsage ? `${requestUsage.total} / ${requestUsage.budget}${requestUsage.exhausted ? " · exhausted" : ""}` : "Unmeasured"],
            ["Requests by phase", requestUsage ? Object.entries(requestUsage.by_phase).map(([phase, count]) => `${label(phase)} ${count}`).join(" · ") : "Unmeasured"],
            ["Transport budget", transportUsage ? `${(transportUsage.budget_consumed_bytes / 1048576).toFixed(2)} / ${(transportUsage.limit_bytes / 1048576).toFixed(2)} MiB${transportUsage.budget_exhausted ? " · exhausted" : ""}` : "Unmeasured"],
            ["Browser payload", transportUsage ? `${(transportUsage.total_bytes / 1048576).toFixed(2)} MiB` : "Unmeasured"],
          ]} />
          <p className="muted small-text">HTTP requests include preparation and workflow, separately from network tunnels. Transport includes encrypted browser payload; the budget also includes vendor control traffic. Provider billing may include additional overhead.</p>
        </section>}
        <div className="actions">
          <button
            className="button secondary"
            onClick={() => setViewer(!viewer)}
          >
            <Monitor size={16} />
            {viewer ? "Hide viewer" : "Open viewer"}
          </button>
          {sessionActive(session.status) && (
            <button
              className="button danger"
              disabled={stop.isPending}
              onClick={() => stop.mutate()}
            >
              <Square size={14} />
              Stop session
            </button>
          )}
        </div>
        <ErrorNotice error={stop.error} />
        {viewer && <SessionViewer token={token} session={session} />}
        <section>
          <h3>Provider observations</h3>
          <p className="muted small-text">
            Verification, provider verdict, and action acceptance are recorded
            independently from workflow completion and ground truth.
          </p>
          <ErrorNotice error={observations.error} />
          {observations.data?.length ? (
            <div className="observation-list">
              {observations.data.map((item) => (
                <div key={item.id}>
                  <div className="actions">
                    <Badge value={item.verification_status} />
                    <Badge
                      value={
                        item.verification_status !== "verified"
                          ? "verdict unavailable"
                          : item.raw_code === 0
                            ? "Clean"
                            : (item.verdict ?? "verdict unavailable")
                      }
                    />
                  </div>
                  <DetailGrid
                    items={[
                      ["Raw vendor code", item.raw_code ?? "Unavailable"],
                      [
                        "Acceptance decision",
                        item.accepted ? "Accepted" : "Not accepted",
                      ],
                      ["Evidence source", label(item.source)],
                      [
                        "Provider version",
                        item.provider_version ?? "Unrecorded",
                      ],
                    ]}
                  />
                </div>
              ))}
            </div>
          ) : (
            <p className="muted small-text">
              No provider observations are recorded for this session.
            </p>
          )}
        </section>
        <section>
          <h3>Retained artifacts</h3>
          <ErrorNotice error={artifacts.error || download.error} />
          {artifacts.data?.length ? (
            <div className="artifact-list">
              {artifacts.data.map((item) => (
                <div key={item.id}>
                  <div>
                    <strong>{item.name}</strong>
                    <small>
                      {(item.size / 1024).toFixed(1)} KB · expires{" "}
                      {dateTime(item.expires_at)}
                    </small>
                    <small className="mono">
                      SHA-256 {item.checksum.slice(0, 16)}…
                    </small>
                  </div>
                  <button
                    className="icon-button"
                    aria-label={`Download ${item.name}`}
                    disabled={download.isPending}
                    onClick={() => download.mutate(item)}
                  >
                    <Download size={16} />
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted small-text">
              No retained artifacts. Capture follows the explicit batch artifact
              policy.
            </p>
          )}
        </section>
        <section>
          <h3>Activity timeline</h3>
          {events.isPending ? (
            <Loading />
          ) : events.error ? (
            <ErrorNotice error={events.error} />
          ) : events.data?.items.length ? (
            <ol className="activity-timeline">
              {events.data.items.map((event, index) => (
                <li key={event.id ?? index}>
                  <span className="timeline-dot" />
                  <div>
                    <strong>{label(event.kind)}</strong>
                    <small>{dateTime(event.created_at)}</small>
                    {event.detail && (
                      <p>
                        {Object.entries(event.detail)
                          .filter(
                            ([key]) =>
                              ![
                                "jpeg_base64",
                                "signature",
                                "token",
                                "password",
                              ].includes(key),
                          )
                          .slice(0, 3)
                          .map(
                            ([key, val]) => `${label(key)}: ${humanValue(val)}`,
                          )
                          .join(" · ")}
                      </p>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted">No recorded events yet.</p>
          )}
        </section>
      </div>
    </aside>
  );
}
function SessionList({
  sessions,
  selected,
  onOpen,
  onSelect,
  selection,
}: {
  sessions: BrowserSession[];
  selected: string | null;
  onOpen: (id: string) => void;
  onSelect: (id: string) => void;
  selection: Set<string>;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // The virtualizer remains outside React Compiler memoization; its scroll state is owned by TanStack.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: sessions.length,
    getScrollElement: () => ref.current,
    estimateSize: () => 72,
    overscan: 6,
  });
  return (
    <>
      <div className="session-table-head">
        <span />
        <span>Session / browser</span>
        <span>Status</span>
        <span>Profile</span>
        <span>Evidence</span>
        <span>Duration</span>
        <span />
      </div>
      <div
        ref={ref}
        className="session-virtual"
        style={{ height: Math.min(540, Math.max(150, sessions.length * 72)) }}
        role="list"
        aria-label="Browser sessions"
      >
        <div
          style={{ height: virtualizer.getTotalSize(), position: "relative" }}
        >
          {virtualizer.getVirtualItems().map((item) => {
            const session = sessions[item.index];
            return (
              <div
                key={session.id}
                role="listitem"
                className={`session-row ${selected === session.id ? "selected" : ""}`}
                style={{
                  height: item.size,
                  transform: `translateY(${item.start}px)`,
                }}
              >
                <input
                  type="checkbox"
                  aria-label={`Select session ${session.id}`}
                  checked={selection.has(session.id)}
                  onChange={() => onSelect(session.id)}
                />
                <button
                  className="session-identity"
                  onClick={() => onOpen(session.id)}
                >
                  <span className="session-avatar">
                    <Monitor size={17} />
                  </span>
                  <span>
                    <strong>{session.adapter}</strong>
                    <small className="mono">{session.id.slice(0, 12)}</small>
                  </span>
                </button>
                <Badge value={session.status} />
                <span className="muted">{label(session.persistence)}</span>
                <Badge value={session.mode} />
                <span className="mono muted">{ms(session.elapsed_ms)}</span>
                <button
                  className="icon-button"
                  aria-label={`Open session ${session.id}`}
                  onClick={() => onOpen(session.id)}
                >
                  <ChevronRight size={17} />
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}
function ProfileHistory({
  token,
  profile,
  close,
}: {
  token: string;
  profile: Profile;
  close: () => void;
}) {
  const query = useQueryClient();
  const [evidence, setEvidence] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const snapshots = useQuery({
    queryKey: platformKey(token, "snapshots", profile.id),
    queryFn: () =>
      api<
        PageResult<{
          id: string;
          generation: number;
          checksum: string;
          environment: unknown;
          created_at: string;
        }>
      >(`/profiles/${profile.id}/snapshots`, token),
  });
  const action = useMutation({
    mutationFn: ({
      command,
      generation,
    }: {
      command: string;
      generation?: number;
    }) =>
      api(`/profiles/${profile.id}/${command}`, token, {
        method: "POST",
        body: {
          generation,
          idempotency_key: crypto.randomUUID(),
          ...(command === "recover"
            ? {
                confirmed_stopped: confirmed,
                ownership_epoch: profile.epoch,
                evidence,
              }
            : {}),
        },
      }),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "profiles"),
      });
      close();
    },
  });
  const available = profile.status === "available" && !profile.owner_session_id;
  return (
    <Modal
      title={profile.name}
      detail="Profile generations and exclusive ownership"
      close={close}
    >
      <div className="modal-body stack">
        <DetailGrid
          items={[
            ["Adapter", profile.adapter],
            ["Status", <Badge key="status" value={profile.status} />],
            ["Generation", profile.version],
            ["Ownership epoch", profile.epoch ?? profile.ownership_epoch ?? 0],
            ["Profile ID", <code key="id">{profile.id}</code>],
            ["Owner", profile.owner_session_id ?? "Unassigned"],
          ]}
        />
        <h3>Encrypted snapshots</h3>
        <ErrorNotice error={snapshots.error || action.error} />
        {snapshots.isPending ? (
          <Loading />
        ) : snapshots.data?.items.length ? (
          <ol className="activity-timeline">
            {snapshots.data.items.map((snapshot) => (
              <li key={snapshot.id}>
                <span className="timeline-dot" />
                <div>
                  <strong>Generation {snapshot.generation}</strong>
                  <small>{dateTime(snapshot.created_at)}</small>
                  <p className="mono">
                    SHA-256 {snapshot.checksum.slice(0, 20)}…
                  </p>
                  <details className="environment-details">
                    <summary>Compatibility environment</summary>
                    <pre>{JSON.stringify(snapshot.environment, null, 2)}</pre>
                  </details>
                  <button
                    className="button secondary small"
                    disabled={!available || action.isPending}
                    onClick={() =>
                      action.mutate({
                        command: "restore",
                        generation: snapshot.generation,
                      })
                    }
                  >
                    Restore this generation
                  </button>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">No clean-shutdown snapshot is recorded.</p>
        )}
        {profile.status === "quarantined" && (
          <fieldset className="choice-fieldset">
            <legend>Recover confirmed stopped runtime</legend>
            <Field
              label="Runtime termination evidence"
              hint="Record the observed process or Kubernetes Job termination and network-route cleanup. Minimum 20 characters."
            >
              <textarea
                minLength={20}
                value={evidence}
                onChange={(e) => setEvidence(e.target.value)}
                rows={3}
              />
            </Field>
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>
                I confirmed the previous browser runtime and its network route
                are stopped.
              </span>
            </label>
            <button
              className="button secondary"
              disabled={
                !confirmed || evidence.trim().length < 20 || action.isPending
              }
              onClick={() => action.mutate({ command: "recover" })}
            >
              Recover profile ownership
            </button>
          </fieldset>
        )}
        <p className="notice">
          Restore and reset require a stopped, available profile. Quarantined
          profiles require runtime reconciliation.
        </p>
      </div>
      <div className="modal-footer">
        <button className="button secondary" onClick={close}>
          Close
        </button>
        <button
          className="button danger"
          disabled={!available || action.isPending}
          onClick={() => action.mutate({ command: "reset" })}
        >
          Reset browser state
        </button>
      </div>
    </Modal>
  );
}
function ProfileForm({ token, close }: { token: string; close: () => void }) {
  const query = useQueryClient();
  const [name, setName] = useState("");
  const [adapter, setAdapter] = useState("playwright");
  const [persistence, setPersistence] = useState("persistent");
  const adapters = useQuery({
    queryKey: platformKey(token, "adapters"),
    queryFn: () => api<Adapter[]>("/adapters", token),
  });
  const create = useMutation({
    mutationFn: () =>
      api("/profiles", token, {
        method: "POST",
        body: { name, adapter, persistence },
      }),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "profiles"),
      });
      close();
    },
  });
  return (
    <Modal
      title="Create browser profile"
      detail="A durable state container with one active writer."
      close={close}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <div className="modal-body stack">
          <Field label="Profile name">
            <input
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label="Browser adapter">
            <Select
              value={adapter}
              onChange={(e) => setAdapter(e.target.value)}
            >
              {adapters.data?.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Persistence">
            <Select
              value={persistence}
              onChange={(e) => setPersistence(e.target.value)}
            >
              <option value="persistent">Persistent</option>
              <option value="fresh">Fresh</option>
            </Select>
          </Field>
          <ErrorNotice error={create.error || adapters.error} />
        </div>
        <div className="modal-footer">
          <button type="button" className="button secondary" onClick={close}>
            Cancel
          </button>
          <button className="button primary" disabled={create.isPending}>
            Create profile
          </button>
        </div>
      </form>
    </Modal>
  );
}
function ProfileList({ token }: { token: string }) {
  const listing = useListState();
  const { offset, setOffset } = listing;
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<Profile | null>(null);
  const profiles = useQuery({
    queryKey: platformKey(token, "profiles", listing.params),
    queryFn: () =>
      api<PageResult<Profile>>(`/profiles?${listing.params}`, token),
  });
  return (
    <section className="panel data-panel">
      <SectionTitle
        title="Durable profiles"
        detail="ABRL browser state, independent of accounts on external platforms."
        action={
          <button
            className="button secondary small"
            onClick={() => setCreating(true)}
          >
            <Plus size={15} />
            New profile
          </button>
        }
      />
      <ListControls label="profiles" state={listing}>
        <Select aria-label="Profile status" value={listing.filters.status ?? ""} onChange={(event) => listing.setFilter("status", event.target.value)}>
          <option value="">All states</option>{["available", "owned", "quarantined"].map((state) => <option key={state} value={state}>{label(state)}</option>)}
        </Select>
        <Select aria-label="Profile persistence" value={listing.filters.persistence ?? ""} onChange={(event) => listing.setFilter("persistence", event.target.value)}><option value="">All profiles</option><option value="fresh">Fresh</option><option value="persistent">Persistent</option></Select>
      </ListControls>
      <ErrorNotice error={profiles.error} />
      {profiles.isPending ? (
        <Loading />
      ) : profiles.data?.items.length ? (
        <>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Browser</th>
                  <th>Persistence</th>
                  <th>Status</th>
                  <th>Generation</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {profiles.data.items.map((profile) => (
                  <tr key={profile.id}>
                    <td>
                      <button
                        className="text-button"
                        onClick={() => setSelected(profile)}
                      >
                        {profile.name}
                      </button>
                    </td>
                    <td>{profile.adapter}</td>
                    <td>{label(profile.persistence)}</td>
                    <td>
                      <Badge value={profile.status} />
                    </td>
                    <td className="mono">{profile.version}</td>
                    <td>
                      <button
                        className="icon-button"
                        aria-label={`View ${profile.name}`}
                        onClick={() => setSelected(profile)}
                      >
                        <ChevronRight size={17} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            total={profiles.data.total}
            offset={offset}
            limit={50}
            onChange={setOffset}
          />
        </>
      ) : (
        <Empty
          title="No browser profiles"
          detail="Create a session batch to create independent fresh or persistent profiles."
        />
      )}
      {selected && (
        <ProfileHistory
          token={token}
          profile={selected}
          close={() => setSelected(null)}
        />
      )}{" "}
      {creating && (
        <ProfileForm token={token} close={() => setCreating(false)} />
      )}
    </section>
  );
}
function BatchCreationDetail({ token, batch, close }: { token: string; batch: Batch; close: () => void }) {
  const [offset, setOffset] = useState(0);
  const key = useRef(crypto.randomUUID());
  const query = useQueryClient();
  const items = useQuery({
    queryKey: platformKey(token, "batch-items", batch.id, offset),
    queryFn: () => api<PageResult<BatchItem>>(`/batches/${batch.id}/items?limit=50&offset=${offset}`, token),
    refetchInterval: 5000,
  });
  const retry = useMutation({
    mutationFn: () => api(`/batches/${batch.id}/retry-creation`, token, {
      method: "POST", headers: { "Idempotency-Key": key.current }, body: {},
    }),
    onSuccess: () => {
      key.current = crypto.randomUUID();
      void query.invalidateQueries({ queryKey: ["platform", token] });
    },
  });
  const failures = batch.creation?.failed ?? 0;
  return (
    <Modal title={`${batch.name} · creation`} detail="One durable allocation record per requested session. Retrying creation never reruns an existing session." close={close} wide>
      <div className="modal-body stack">
        <DetailGrid items={[
          ["Requested", batch.count], ["Created", batch.creation?.created ?? 0],
          ["Creation failures", failures], ["Cancelled allocations", batch.creation?.cancelled ?? 0],
        ]} />
        {failures > 0 && <div className="notice">Creation failures remain visible separately from workflow outcomes. A paused batch stays paused after retry.</div>}
        <ErrorNotice error={items.error || retry.error} />
        {items.isPending ? <Loading /> : items.data?.items.length ? <>
          <div className="table-scroll"><table>
            <thead><tr><th>Item</th><th>Browser</th><th>Allocation</th><th>Attempts</th><th>Session / error</th></tr></thead>
            <tbody>{items.data.items.map((item) => <tr key={item.ordinal}>
              <td>{item.ordinal}</td><td>{item.adapter}</td><td><Badge value={item.status} /></td><td>{item.attempts}</td>
              <td>{item.error || (item.session_id ? <span className="mono">{item.session_id}</span> : "No session allocated")}</td>
            </tr>)}</tbody>
          </table></div>
          <Pagination total={items.data.total} offset={offset} limit={50} onChange={setOffset} />
        </> : <Empty title="No allocation records" detail="Historical batches may predate per-item creation records." />}
      </div>
      <div className="modal-footer">
        <button className="button secondary" onClick={close}>Close</button>
        <button className="button primary" disabled={!failures || retry.isPending || ["completed", "cancelled", "cancelling"].includes(batch.status)} onClick={() => retry.mutate()}>
          {retry.isPending ? "Retrying creation…" : "Retry failed creation"}
        </button>
      </div>
    </Modal>
  );
}

export function Sessions({
  token,
  targets,
  workflows,
}: {
  token: string;
  targets: Target[];
  workflows: Workflow[];
}) {
  const [tab, setTab] = useState<"sessions" | "batches" | "profiles">(
    "sessions",
  );
  const [batchFilter, setBatchFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const [creationDetail, setCreationDetail] = useState<string | null>(null);
  const listing = useListState();
  const batchListing = useListState();
  const { offset, setOffset, search, setSearch } = listing;
  const status = listing.filters.status ?? "";
  const setStatus = (value: string) => listing.setFilter("status", value);
  const [selected, setSelected] = useState<string | null>(null);
  const [selection, setSelection] = useState(new Set<string>());
  const query = useQueryClient();
  const sessions = useQuery({
    queryKey: platformKey(
      token,
      "sessions",
      offset,
      status,
      search,
      batchFilter,
      listing.params,
    ),
    queryFn: () =>
      api<PageResult<BrowserSession>>(
        `/sessions?${listing.params}${batchFilter ? `&batch_id=${batchFilter}` : ""}`,
        token,
      ),
    refetchInterval: 3000,
    enabled: tab === "sessions",
  });
  const batches = useQuery({
    queryKey: platformKey(token, "batches", batchListing.params),
    queryFn: () => api<PageResult<Batch>>(`/batches?${batchListing.params}`, token),
    refetchInterval: 5000,
    enabled: tab === "batches",
  });
  const batchAction = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      api(`/batches/${id}/${action}`, token, {
        method: "POST",
        body: { idempotency_key: crypto.randomUUID() },
      }),
    onSuccess: () =>
      void query.invalidateQueries({ queryKey: ["platform", token] }),
  });
  const bulk = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled(
        [...selection].map((id) =>
          api(`/sessions/${id}/commands`, token, {
            method: "POST",
            body: { command: "stop", idempotency_key: crypto.randomUUID() },
          }),
        ),
      );
      const failures = results.filter((r) => r.status === "rejected");
      if (failures.length)
        throw new Error(
          `${failures.length} of ${results.length} stop requests failed. Refresh and retry those sessions.`,
        );
    },
    onSettled: () => {
      setSelection(new Set());
      void query.invalidateQueries({
        queryKey: platformKey(token, "sessions"),
      });
    },
  });
  const active = (sessions.data?.items ?? []).find((s) => s.id === selected);
  return (
    <div className="stack">
      <div className="page-toolbar">
        <Segments
          label="Session views"
          value={tab}
          onChange={setTab}
          options={[
            { value: "sessions", label: "Sessions" },
            { value: "batches", label: "Batches" },
            { value: "profiles", label: "Profiles" },
          ]}
        />
        <button className="button primary" onClick={() => setCreating(true)}>
          <CopyPlus size={16} />
          Create batch
        </button>
      </div>
      {tab === "sessions" && (
        <section className="panel data-panel">
          <div className="filter-bar">
            <div className="search-field">
              <Search size={17} />
              <input
                aria-label="Search sessions"
                placeholder="Search session ID"
                maxLength={100}
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setOffset(0);
                }}
              />
            </div>
            <Select
              aria-label="Session status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">All statuses</option>
              {[
                "queued",
                "starting",
                "ready",
                "running",
                "preparing",
                "awaiting_operator",
                "stopping",
                "completed",
                "failed",
                "blocked",
                "cancelled",
                "needs_reconciliation",
              ].map((value) => (
                <option key={value} value={value}>
                  {label(value)}
                </option>
              ))}
            </Select>
            <Select aria-label="Session evidence" value={listing.filters.mode ?? ""} onChange={(event) => listing.setFilter("mode", event.target.value)}><option value="">All evidence</option><option value="live">Live</option><option value="demo">Demo</option></Select>
            <Select aria-label="Session browser" value={listing.filters.adapter ?? ""} onChange={(event) => listing.setFilter("adapter", event.target.value)}><option value="">All browsers</option>{["playwright", "manual_chrome", "cloakbrowser", "kameleo"].map((adapter) => <option key={adapter} value={adapter}>{label(adapter)}</option>)}</Select>
            <Select aria-label="Sort sessions" value={listing.order} onChange={(event) => listing.setOrder(event.target.value)}><option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="status_asc">State</option><option value="duration_desc">Longest duration</option></Select>
            <span className="muted mono small-text">
              {sessions.data?.total ?? 0} sessions
            </span>
          </div>
          {batchFilter && (
            <div className="selection-bar">
              <span>Batch {batchFilter.slice(0, 12)}</span>
              <button
                className="text-button"
                onClick={() => setBatchFilter("")}
              >
                Show all sessions
              </button>
            </div>
          )}
          {selection.size > 0 && (
            <div className="selection-bar">
              <Check size={16} />
              <strong>{selection.size} selected</strong>
              <button
                className="button danger small"
                disabled={bulk.isPending}
                onClick={() => bulk.mutate()}
              >
                Stop selected
              </button>
              <button
                className="text-button"
                onClick={() => setSelection(new Set())}
              >
                Clear
              </button>
            </div>
          )}
          <ErrorNotice error={sessions.error || bulk.error} />
          {sessions.isPending ? (
            <Loading />
          ) : sessions.data?.items.length ? (
            <>
              <SessionList
                sessions={sessions.data.items}
                selected={selected}
                onOpen={setSelected}
                selection={selection}
                onSelect={(id) =>
                  setSelection((current) => {
                    const next = new Set(current);
                    if (next.has(id)) next.delete(id);
                    else next.add(id);
                    return next;
                  })
                }
              />
              <Pagination
                total={sessions.data.total}
                offset={offset}
                limit={50}
                onChange={setOffset}
              />
            </>
          ) : (
            <Empty
              title="Your browser fleet starts here"
              detail="Create a batch to run independent sessions on one workflow, with explicit profile, proxy, and preparation policies."
              action={
                <button
                  className="button secondary"
                  onClick={() => setCreating(true)}
                >
                  <Plus size={16} />
                  Create first batch
                </button>
              }
            />
          )}
        </section>
      )}
      {tab === "batches" && (
        <section className="panel data-panel">
          <SectionTitle
            title="Session batches"
            detail="Resumable creation, progress, and per-session outcomes."
          />
          <ErrorNotice error={batches.error || batchAction.error} />
          <ListControls label="batches" state={batchListing}>
            <Select aria-label="Batch status" value={batchListing.filters.status ?? ""} onChange={(event) => batchListing.setFilter("status", event.target.value)}><option value="">All states</option>{["queued", "running", "paused", "creation_failed", "completed", "cancelled"].map((state) => <option key={state} value={state}>{label(state)}</option>)}</Select>
            <Select aria-label="Batch evidence" value={batchListing.filters.mode ?? ""} onChange={(event) => batchListing.setFilter("mode", event.target.value)}><option value="">All evidence</option><option value="live">Live</option><option value="demo">Demo</option></Select>
          </ListControls>
          {batches.isPending ? (
            <Loading />
          ) : batches.data?.items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Batch</th>
                    <th>Status</th>
                    <th>Sessions</th>
                    <th>Allocation</th>
                    <th>Completed</th>
                    <th>Failed</th>
                    <th>Created</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {batches.data.items.map((batch) => (
                    <tr key={batch.id}>
                      <td>
                        <button
                          className="text-button"
                          onClick={() => {
                            setBatchFilter(batch.id);
                            setOffset(0);
                            setTab("sessions");
                          }}
                        >
                          <strong>{batch.name}</strong>
                        </button>
                        <small className="table-subtitle mono">
                          {batch.id.slice(0, 12)}
                        </small>
                      </td>
                      <td>
                        <Badge value={batch.status} />
                      </td>
                      <td>{batch.count ?? batch.definition?.count ?? "—"}</td>
                      <td>
                        {batch.creation?.created ?? 0} created
                        <small className="table-subtitle">{batch.creation?.failed ?? 0} creation failures</small>
                      </td>
                      <td>
                        {batch.completed ??
                          batch.counts?.completed ??
                          batch.progress?.completed ??
                          0}
                      </td>
                      <td>
                        {batch.failed ??
                          batch.counts?.failed ??
                          batch.progress?.failed ??
                          0}
                      </td>
                      <td>{dateTime(batch.created_at)}</td>
                      <td>
                        <button className="button secondary small" aria-label={`Creation details for ${batch.name}`} onClick={() => setCreationDetail(batch.id)}>Creation details</button>
                        {!["completed", "cancelled"].includes(batch.status) && (
                          <div className="actions">
                            <button
                              className="icon-button"
                              aria-label={`${batch.status === "paused" ? "Resume" : "Pause"} ${batch.name}`}
                              disabled={batchAction.isPending}
                              onClick={() =>
                                batchAction.mutate({
                                  id: batch.id,
                                  action:
                                    batch.status === "paused"
                                      ? "resume"
                                      : "pause",
                                })
                              }
                            >
                              {batch.status === "paused" ? (
                                <Play size={15} />
                              ) : (
                                <Pause size={15} />
                              )}
                            </button>
                            <button
                              className="icon-button"
                              aria-label={`Cancel ${batch.name}`}
                              disabled={batchAction.isPending}
                              onClick={() =>
                                batchAction.mutate({
                                  id: batch.id,
                                  action: "cancel",
                                })
                              }
                            >
                              <Square size={14} />
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title="No batches yet"
              detail="Batch creation keeps a durable record of every requested session."
            />
          )}
          {batches.data && <Pagination total={batches.data.total} offset={batchListing.offset} limit={50} onChange={batchListing.setOffset} />}
        </section>
      )}
      {tab === "profiles" && <ProfileList token={token} />}
      <div className="context-note">
        <ShieldCheck size={16} />
        <span>
          One writable owner per persistent profile. Browser state is saved only
          after a supported clean shutdown.
        </span>
        <Clock3 size={15} />
        <span>One shared refresh for this list.</span>
      </div>
      {active && (
        <SessionDetail
          token={token}
          session={active}
          close={() => setSelected(null)}
        />
      )}{" "}
      {creationDetail && batches.data?.items.find((batch) => batch.id === creationDetail) && (
        <BatchCreationDetail token={token} batch={batches.data.items.find((batch) => batch.id === creationDetail)!} close={() => setCreationDetail(null)} />
      )}
      {creating && (
        <BatchWizard
          token={token}
          targets={targets}
          workflows={workflows}
          close={() => setCreating(false)}
          onCreated={() => setTab("sessions")}
        />
      )}
    </div>
  );
}
