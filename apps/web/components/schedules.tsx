"use client";

import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarClock,
  Clock3,
  History,
  Pause,
  Play,
  Plus,
} from "lucide-react";
import { api, dateTime, label, validateWorkflowUrlOverrides, workflowUrlParameterNames } from "@/lib/api";
import {
  type Adapter,
  type Schedule,
  type ScheduleInput,
  type Occurrence,
  type PageResult,
  type ProxyPolicy,
  type SharedProps,
  platformKey,
  configuredDraft,
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
import { DetailGrid, ListControls, Modal, Pagination, useListState } from "./platform-ui";
import { BatchFields, batchBrowserSettingsValid, defaultBatch } from "./sessions";
import { BatchReadinessNotices, useBatchPreflight, WorkflowValuesFields } from "./batch-setup";
import type { Target, Workflow } from "@/lib/api";
import { ScheduleCalendar } from "./schedule-calendar";
import { WorkflowUrlParameters } from "./workflow-url-parameters";

function scheduleNeedsRevision(schedule?: Schedule): boolean {
  return Boolean(schedule?.pause_reason || schedule?.batch.mode === "demo");
}

function ScheduleForm({
  token,
  targets,
  workflows,
  close,
  initial,
}: SharedProps & { close: () => void; initial?: Schedule }) {
  const needsRevision = scheduleNeedsRevision(initial);
  const savedSessionSettings = initial?.batch.session_groups?.length && !needsRevision
    ? initial.batch : undefined;
  const [value, setValue] = useState<ScheduleInput>(() => {
    const defaults = defaultBatch(targets, workflows);
    const draft = configuredDraft<ScheduleInput>(
      {
        name: "Scheduled workflow",
        kind: "interval",
        timezone: "Asia/Jerusalem",
        interval_seconds: 3600,
        batch: defaults,
        refresh_dependencies: false,
        overlap: "skip",
        misfire: "coalesce",
        grace_seconds: 900,
        start_at: undefined,
        end_at: undefined,
        expression: undefined,
      },
      initial,
    );
    if (needsRevision) {
      draft.batch = {
        ...defaults,
        name: draft.batch.name,
        target_id: targets.some((site) => site.id === draft.batch.target_id && site.live_ready !== false)
          ? draft.batch.target_id : "",
        workflow_id: draft.batch.workflow_id,
        count: draft.batch.count,
        concurrency: draft.batch.concurrency,
        adapters: draft.batch.adapters,
        persistence: draft.batch.persistence,
        proxy_policy_id: draft.batch.proxy_policy_id,
      };
    }
    return draft;
  });
  const [showBatch, setShowBatch] = useState(needsRevision);
  const [extraTarget, setExtraTarget] = useState<Target>();
  const [extraWorkflow, setExtraWorkflow] = useState<Workflow>();
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
  const selectedTarget = targets.find((target) => target.id === value.batch.target_id) ?? (extraTarget?.id === value.batch.target_id ? extraTarget : undefined);
  const selectedWorkflow = workflows.find((workflow) => workflow.id === value.batch.workflow_id) ?? (extraWorkflow?.id === value.batch.workflow_id ? extraWorkflow : undefined);
  const pinned = Boolean(value.batch.session_template_id);
  const steps = selectedWorkflow?.steps ?? [];
  const hasWorkflowParameters = !pinned && workflowUrlParameterNames(steps).length > 0;
  const overrideValues = value.batch.fixture_params ?? {};
  const { overrides, error: parameterError } = pinned
    ? { overrides: overrideValues, error: undefined }
    : validateWorkflowUrlOverrides(steps, overrideValues);
  const input = {
    ...value.batch,
    expected_workflow_version: pinned ? undefined : selectedWorkflow?.version,
    fixture_params: Object.keys(overrides).length > 0 ? overrides : undefined,
    expected_dependencies: undefined,
  };
  const configured = !parameterError && value.batch.mode !== "demo" && Boolean(selectedTarget && selectedWorkflow)
    && batchBrowserSettingsValid(value.batch, adapters.data ?? []);
  const changed = !savedSessionSettings && (!initial || needsRevision || Boolean(value.refresh_dependencies)
    || JSON.stringify(value.batch) !== JSON.stringify(initial.batch));
  const readiness = useBatchPreflight(token, input, configured && changed);
  const valid = !changed || (configured && readiness.current && Boolean(readiness.data) && !readiness.error);
  const body = {
    ...value,
    ...(savedSessionSettings ? { refresh_dependencies: false } : {}),
    batch: savedSessionSettings ?? (changed ? {
      ...input, expected_dependencies: readiness.data?.expected_dependencies,
    } : value.batch),
  };
  const preview = useMutation({
    mutationFn: () =>
      api<{ occurrences: Array<{ utc: string; local: string }> }>(
        "/schedules/preview",
        token,
        { method: "POST", body },
      ),
  });
  const save = useMutation({
    mutationFn: () =>
      api(initial ? `/schedules/${initial.id}` : "/schedules", token, {
        method: initial ? "PUT" : "POST",
        body,
      }),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "schedules"),
      });
      close();
    },
  });
  const times = preview.data?.occurrences ?? [];
  const patch = (next: Partial<ScheduleInput>) =>
    setValue({ ...value, ...next });
  function submit(e: FormEvent) {
    e.preventDefault();
    if (valid) save.mutate();
  }
  return (
    <Modal
      title={initial ? "Edit schedule" : "Schedule a workflow"}
      detail="A durable schedule with pinned browser, site, workflow, and network settings."
      close={close}
      wide
    >
      <form onSubmit={submit}>
        <div className="modal-body stack">
          {needsRevision && <p className="notice">{initial?.pause_reason ?? "This schedule previously used simulated execution."} Save a live configuration, then resume the schedule.</p>}
          <div className="form-grid">
            <Field label="Schedule name">
              <input
                required
                value={value.name}
                onChange={(e) => patch({ name: e.target.value })}
              />
            </Field>
            <Field label="Schedule type">
              <Select
                value={value.kind}
                onChange={(e) =>
                  patch({ kind: e.target.value as ScheduleInput["kind"] })
                }
              >
                <option value="once">One time</option>
                <option value="interval">Fixed interval</option>
                <option value="cron">Calendar · cron expression</option>
              </Select>
            </Field>
            <Field label="IANA timezone">
              <input
                required
                value={value.timezone}
                onChange={(e) => patch({ timezone: e.target.value })}
                list="timezones"
              />
              <datalist id="timezones">
                <option>Asia/Jerusalem</option>
                <option>UTC</option>
                <option>America/New_York</option>
                <option>Europe/London</option>
              </datalist>
            </Field>
            {value.kind === "interval" && (
              <Field label="Interval in seconds" hint="Minimum 60 seconds.">
                <input
                  required
                  type="number"
                  min={60}
                  value={value.interval_seconds ?? 3600}
                  onChange={(e) =>
                    patch({ interval_seconds: Number(e.target.value) })
                  }
                />
              </Field>
            )}
            {value.kind === "cron" && (
              <Field
                label="Cron expression"
                hint="Minute · hour · day · month · weekday. Use named weekdays, e.g. mon-fri."
              >
                <input
                  required
                  className="mono"
                  placeholder="0 9 * * mon-fri"
                  value={value.expression ?? ""}
                  onChange={(e) => patch({ expression: e.target.value })}
                />
              </Field>
            )}
            <Field
              label={
                value.kind === "once"
                  ? "Execution timestamp"
                  : "Start timestamp (optional)"
              }
              hint="ISO 8601 including an offset, e.g. 2026-11-15T09:00:00+02:00."
            >
              <input
                required={value.kind === "once"}
                placeholder="YYYY-MM-DDTHH:mm:ss+02:00"
                value={value.start_at ?? ""}
                onChange={(e) =>
                  patch({ start_at: e.target.value || undefined })
                }
              />
            </Field>
            <Field
              label="Expiration timestamp (optional)"
              hint="ISO 8601 including an offset; later than the start timestamp."
            >
              <input
                placeholder="YYYY-MM-DDTHH:mm:ss+02:00"
                value={value.end_at ?? ""}
                onChange={(e) => patch({ end_at: e.target.value || undefined })}
              />
            </Field>
            <Field label="Overlap behavior">
              <Select
                value={value.overlap}
                onChange={(e) =>
                  patch({ overlap: e.target.value as ScheduleInput["overlap"] })
                }
              >
                <option value="skip">
                  Skip while previous batch is active
                </option>
                <option value="queue_one">Queue one occurrence</option>
                <option value="allow">Allow within concurrency limits</option>
              </Select>
            </Field>
            <Field label="Missed occurrences">
              <Select
                value={value.misfire}
                onChange={(e) =>
                  patch({ misfire: e.target.value as ScheduleInput["misfire"] })
                }
              >
                <option value="coalesce">
                  Coalesce one within grace period
                </option>
                <option value="skip">Skip missed occurrences</option>
              </Select>
            </Field>
            <Field label="Grace period in seconds">
              <input
                required
                type="number"
                min={0}
                max={86400}
                value={value.grace_seconds}
                onChange={(e) =>
                  patch({ grace_seconds: Number(e.target.value) })
                }
              />
            </Field>
          </div>
          <div className="notice">
            <Clock3 size={17} /> Nonexistent daylight-saving times are skipped.
            Repeated local times execute once, at their first occurrence.
          </div>
          <div className="subsection-header">
            <h3>{savedSessionSettings ? "Workflow & sessions" : "Workflow & session template"}</h3>
            {!savedSessionSettings && <button
              type="button"
              className="button secondary small"
              onClick={() => setShowBatch(!showBatch)}
            >
              {showBatch ? "Collapse settings" : "Configure sessions"}
            </button>}
          </div>
          {savedSessionSettings && <p className="muted small-text">This schedule runs with its saved per-session settings. You can adjust its timing here.</p>}
          {showBatch && !savedSessionSettings ? (
            <BatchFields
              token={token}
              value={value.batch}
              onChange={(batch, template) => {
                const workflowChanged = batch.workflow_id !== value.batch.workflow_id;
                const detachedDemo = value.batch.mode === "demo" && !batch.session_template_id && !template;
                patch({ batch: {
                  ...batch,
                  ...(workflowChanged && !template ? {
                    fixture_params: undefined, workflow_inputs: undefined,
                  } : {}),
                  ...(detachedDemo ? { mode: undefined, preparation: undefined, seed: undefined } : {}),
                } });
              }}
              targets={extraTarget && !targets.some((target) => target.id === extraTarget.id) ? [...targets, extraTarget] : targets}
              workflows={extraWorkflow && !workflows.some((workflow) => workflow.id === extraWorkflow.id) ? [...workflows, extraWorkflow] : workflows}
              onTarget={setExtraTarget}
              onWorkflow={setExtraWorkflow}
              adapters={adapters.data ?? []}
              policies={policies.data?.items ?? []}
              sessionTemplates
              launchParameters={<>
                {!pinned && <WorkflowValuesFields workflow={selectedWorkflow}
                  value={value.batch.workflow_inputs ?? {}} onChange={(workflow_inputs) => patch({ batch: { ...value.batch, workflow_inputs } })} />}
                {hasWorkflowParameters && <WorkflowUrlParameters steps={steps} value={overrideValues} origin={selectedTarget?.origin}
                  onChange={(fixture_params) => patch({ batch: { ...value.batch, fixture_params } })} />}
              </>}
            />
          ) : (
            <DetailGrid
              items={[
                [
                  "Site",
                  targets.find((t) => t.id === value.batch.target_id)?.name ??
                    "Select a site",
                ],
                [
                  "Workflow",
                  workflows.find((w) => w.id === value.batch.workflow_id)
                    ?.name ?? "Select a workflow",
                ],
                [
                  "Sessions",
                  `${value.batch.count} · ${value.batch.concurrency} concurrent`,
                ],
              ]}
            />
          )}
          {initial?.dependencies && <p className="muted small-text">
            Pinned site v{initial.dependencies.target_version} · workflow v{initial.dependencies.workflow_version}
          </p>}
          {initial && !pinned && !savedSessionSettings && <label className="checkbox-line">
            <input type="checkbox" checked={value.refresh_dependencies ?? false}
              onChange={(event) => patch({ refresh_dependencies: event.target.checked })} />
            Refresh pinned dependency versions
          </label>}
          {readiness.current && readiness.isFetching && <p role="status">Checking batch readiness…</p>}
          {readiness.current && <ErrorNotice error={readiness.error} />}
          {readiness.current && readiness.data && <BatchReadinessNotices notices={readiness.data.notices} />}
          {value.batch.mode === "demo" && <p className="notice">Detach this simulated template to configure live sessions.</p>}
          <div className="subsection-header">
            <h3>Upcoming executions</h3>
            <button
              type="button"
              className="button secondary small"
              disabled={preview.isPending}
              onClick={() => preview.mutate()}
            >
              <CalendarClock size={15} />
              {preview.isPending ? "Calculating…" : "Preview schedule"}
            </button>
          </div>
          {times.length > 0 ? (
            <ol className="schedule-preview">
              {times.map((time) => (
                <li key={time.utc}>
                  <Clock3 size={15} />
                  <time dateTime={time.utc}>
                    {new Intl.DateTimeFormat("en", {
                      dateStyle: "medium",
                      timeStyle: "short",
                      timeZone: value.timezone,
                    }).format(new Date(time.utc))}
                  </time>
                  <span className="muted">{value.timezone}</span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted small-text">
              Preview uses the same trigger calculations as the scheduler.
            </p>
          )}
          <ErrorNotice
            error={save.error || preview.error || adapters.error || policies.error || parameterError}
          />
        </div>
        <div className="modal-footer">
          <button type="button" className="button secondary" onClick={close}>
            Cancel
          </button>
          <button
            className="button primary"
            disabled={save.isPending || !valid}
          >
            <Plus size={16} />
            {save.isPending
              ? "Saving…"
              : initial
                ? "Save schedule version"
                : "Create schedule"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
function ScheduleHistory({
  token,
  schedule,
  close,
}: {
  token: string;
  schedule: Schedule;
  close: () => void;
}) {
  const events = useQuery({
    queryKey: platformKey(token, "occurrences", schedule.id),
    queryFn: () =>
      api<PageResult<Occurrence>>(
        `/schedules/${schedule.id}/occurrences?limit=100`,
        token,
      ),
  });
  return (
    <Modal title={schedule.name} detail="Execution history" close={close}>
      <div className="modal-body">
        {schedule.pause_reason && <p className="notice">{schedule.pause_reason}</p>}
        <DetailGrid
          items={[
            ["Timezone", schedule.timezone],
            ["Overlap", label(schedule.overlap)],
            ["Missed occurrences", label(schedule.misfire)],
            [
              "Status",
              <Badge
                key="status"
                value={
                  schedule.enabled === false
                    ? "paused"
                    : (schedule.status ?? "active")
                }
              />,
            ],
          ]}
        />
        <ErrorNotice error={events.error} />
        {events.isPending ? (
          <Loading />
        ) : events.data?.items.length ? (
          <ol className="activity-timeline">
            {events.data.items.map((item) => (
              <li key={item.id}>
                <span className="timeline-dot" />
                <div>
                  <Badge value={item.status} />
                  <small>
                    {dateTime(
                      item.scheduled_for ??
                        item.scheduled_at ??
                        item.created_at,
                    )}
                  </small>
                  {item.batch_id && (
                    <p className="mono">Batch {item.batch_id.slice(0, 12)}</p>
                  )}
                  {(item.error || item.reason) && (
                    <p>{item.error || item.reason}</p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <Empty
            title="No occurrences yet"
            detail="Executed, skipped, and coalesced occurrences will appear here."
          />
        )}
      </div>
    </Modal>
  );
}
export function Schedules(props: SharedProps) {
  const { token } = props;
  const [creating, setCreating] = useState(false);
  const [history, setHistory] = useState<Schedule | null>(null);
  const [editing, setEditing] = useState<Schedule>();
  const listing = useListState();
  const { offset, setOffset } = listing;
  const query = useQueryClient();
  const schedules = useQuery({
    queryKey: platformKey(token, "schedules", listing.params),
    queryFn: () =>
      api<PageResult<Schedule>>(`/schedules?${listing.params}`, token),
    refetchInterval: 10000,
  });
  const action = useMutation({
    mutationFn: ({ id, command }: { id: string; command: string }) =>
      api(`/schedules/${id}/${command}`, token, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      }),
    onSuccess: () =>
      void query.invalidateQueries({
        queryKey: platformKey(token, "schedules"),
      }),
  });
  return (
    <div className="stack">
      <div className="page-toolbar">
        <span className="context-note">
          <CalendarClock size={17} />
          Persistent occurrences, explicit overlap rules.
        </span>
        <button className="button primary" onClick={() => setCreating(true)}>
          <Plus size={16} />
          New schedule
        </button>
      </div>
      <ErrorNotice error={action.error} />
      {schedules.data && <ScheduleCalendar schedules={schedules.data.items} total={schedules.data.total} open={(schedule) => { setEditing(schedule); setCreating(true); }} />}
      <section className="panel data-panel">
        <SectionTitle
          title="Scheduled work"
          detail="Schedules continue independently of this browser tab."
        />
        <ListControls label="schedules" state={listing} orders={[{value:"newest",label:"Newest first"},{value:"oldest",label:"Oldest first"},{value:"next_run",label:"Next execution"},{value:"name_asc",label:"Name A–Z"}]}>
          <Select aria-label="Schedule state" value={listing.filters.enabled ?? ""} onChange={(event) => listing.setFilter("enabled", event.target.value)}><option value="">All states</option><option value="true">Enabled</option><option value="false">Paused</option></Select>
          <Select aria-label="Schedule type" value={listing.filters.kind ?? ""} onChange={(event) => listing.setFilter("kind", event.target.value)}><option value="">All types</option><option value="once">One-time</option><option value="interval">Interval</option><option value="cron">Calendar</option></Select>
        </ListControls>
        <ErrorNotice error={schedules.error} />
        {schedules.isPending ? (
          <Loading />
        ) : schedules.data?.items.length ? (
          <>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Schedule</th>
                    <th>Status</th>
                    <th>Trigger</th>
                    <th>Next execution</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {schedules.data.items.map((schedule) => (
                    <tr key={schedule.id}>
                      <td>
                        <button
                          className="text-button"
                          onClick={() => {
                            setEditing(schedule);
                            setCreating(true);
                          }}
                        >
                          <strong>{schedule.name}</strong>
                        </button>
                        <small className="table-subtitle">
                          {schedule.timezone}
                        </small>
                      </td>
                      <td>
                        <Badge
                          value={
                            schedule.enabled === false
                              ? "paused"
                              : (schedule.status ?? "active")
                          }
                        />
                        {schedule.pause_reason && <small className="table-subtitle">{schedule.pause_reason}</small>}
                      </td>
                      <td>
                        {schedule.kind === "interval"
                          ? `Every ${schedule.interval_seconds}s`
                          : (schedule.expression ?? label(schedule.kind))}
                      </td>
                      <td>
                        {schedule.next_run_at || schedule.next_fire_at
                          ? dateTime(
                              (schedule.next_run_at || schedule.next_fire_at)!,
                            )
                          : "—"}
                      </td>
                      <td>
                        <div className="actions">
                          <button
                            className="icon-button"
                            aria-label={`Run ${schedule.name} now`}
                            disabled={action.isPending || scheduleNeedsRevision(schedule)}
                            onClick={() =>
                              action.mutate({
                                id: schedule.id,
                                command: "run-now",
                              })
                            }
                          >
                            <Play size={16} />
                          </button>
                          <button
                            className="icon-button"
                            aria-label={`${schedule.enabled === false ? "Resume" : "Pause"} ${schedule.name}`}
                            disabled={action.isPending || scheduleNeedsRevision(schedule)}
                            onClick={() =>
                              action.mutate({
                                id: schedule.id,
                                command:
                                  schedule.enabled === false
                                    ? "resume"
                                    : "pause",
                              })
                            }
                          >
                            {schedule.enabled === false ? (
                              <Play size={16} />
                            ) : (
                              <Pause size={16} />
                            )}
                          </button>
                          <button
                            className="icon-button"
                            aria-label={`View ${schedule.name} history`}
                            onClick={() => setHistory(schedule)}
                          >
                            <History size={16} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              total={schedules.data.total}
              offset={offset}
              limit={50}
              onChange={setOffset}
            />
          </>
        ) : (
          <Empty
            title="Make work repeatable"
            detail="Schedule one-time, interval, or calendar batches with pinned configurations and a durable occurrence history."
            action={
              <button
                className="button secondary"
                onClick={() => setCreating(true)}
              >
                <Plus size={16} />
                Create first schedule
              </button>
            }
          />
        )}
      </section>
      {creating && (
        <ScheduleForm
          {...props}
          initial={editing}
          close={() => {
            setCreating(false);
            setEditing(undefined);
          }}
        />
      )}{" "}
      {history && (
        <ScheduleHistory
          token={token}
          schedule={history}
          close={() => setHistory(null)}
        />
      )}
    </div>
  );
}
