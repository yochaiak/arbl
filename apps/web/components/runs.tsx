"use client";

import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, getPaginationRowModel, useReactTable } from "@tanstack/react-table";
import { Archive, ArrowDownToLine, ArrowLeft, ArrowRight, ChevronLeft, ChevronRight, CircleStop, Clock3, Gauge, Pause, Play, ScanLine, X } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, dateTime, exportRun, isActive, label, ms, pct, streamRunEvents, type Report, type RunEvent, type Run, type RunInput, type Target, type Trial, type Workflow } from "@/lib/api";
import { Badge, Empty, ErrorNotice, Field, Loading, SectionTitle, Select } from "./ui";
import { ModeBadge } from "./registry";

export function NewRun({ token, targets, workflows, preview, close, onRun }: {
  token: string;
  targets: Target[];
  workflows: Workflow[];
  preview: Workflow | null;
  close: () => void;
  onRun: (run: Run) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [form, setForm] = useState<RunInput>({
    target_id: targets[0]?.id ?? "",
    workflow_id: preview?.id ?? workflows[0]?.id ?? "",
    repetitions: preview ? 1 : 30,
    seed: 42,
    phase: "pilot",
    mode: preview && targets[0]?.verified ? "live" : "demo",
    reliability_target: 0.95,
  });
  const query = useQueryClient();
  const target = targets.find((entry) => entry.id === form.target_id);
  const create = useMutation({
    mutationFn: () => api<Run>("/runs", token, { method: "POST", body: form }),
    onSuccess: (run) => {
      query.setQueryData<Run[]>(["runs", token], (items = []) => [run, ...items.filter((item) => item.id !== run.id)]);
      void query.invalidateQueries({ queryKey: ["runs", token] });
      onRun(run);
    },
  });

  useEffect(() => { dialog.current?.showModal(); }, []);

  function set<K extends keyof RunInput>(key: K, value: RunInput[K]) {
    setForm((previous) => ({
      ...previous,
      [key]: value,
      ...(key === "phase" && !preview ? { repetitions: value === "pilot" ? 30 : 300 } : {}),
    }));
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate();
  }

  const needsProof = form.mode === "live" && target?.verified === false;

  return <dialog ref={dialog} className="dialog" aria-labelledby="run-dialog-title" onCancel={(event) => { event.preventDefault(); if (!create.isPending) close(); }}>
    <form className="edit-form" onSubmit={submit}>
      <div className="dialog-title">
        <div><h2 id="run-dialog-title">{preview ? "Preview workflow" : "Browser-only run"}</h2><p>{preview ? "Execute one trial against the selected site." : "Choose the site, workflow, and sample size."}</p></div>
        <button type="button" className="icon-button bare" aria-label="Close" onClick={close} disabled={create.isPending}><X size={20} /></button>
      </div>
      {(!targets.length || !workflows.length) && <div className="notice">Register a site and save a workflow, or load the starter suite from Overview.</div>}
      <div className="notice">This preview measures observable workflow completion. Use session batches or research experiments for server-verified AdScore evidence.</div>
      <div className="form-grid">
        <Field label="Site"><Select required value={form.target_id} onChange={(event) => set("target_id", event.target.value)}>
          {!targets.length && <option value="">No sites registered</option>}{targets.map((site) => <option key={site.id} value={site.id}>{site.name}</option>)}
        </Select></Field>
        <Field label="Workflow"><Select required value={form.workflow_id} onChange={(event) => set("workflow_id", event.target.value)}>
          {!workflows.length && <option value="">No workflows saved</option>}{workflows.map((workflow) => <option key={workflow.id} value={workflow.id}>{workflow.name}</option>)}
        </Select></Field>
        <Field label="Evidence source"><Select value={form.mode} onChange={(event) => set("mode", event.target.value as Run["mode"])}><option value="demo">Simulated demo</option><option value="live">Live browser execution</option></Select></Field>
        {!preview && <Field label="Phase"><Select value={form.phase} onChange={(event) => set("phase", event.target.value as Run["phase"])}><option value="pilot">Pilot</option><option value="confirmation">Confirmation</option></Select></Field>}
        {!preview && <Field label="Trials"><input type="number" min={1} max={300} required value={form.repetitions} onChange={(event) => set("repetitions", event.target.valueAsNumber)} /></Field>}
        <Field label="Random seed"><input type="number" min={0} max={2147483647} required value={form.seed} onChange={(event) => set("seed", event.target.valueAsNumber)} /></Field>
        {!preview && <Field label="Reliability target (%)"><input type="number" min={0.1} max={100} step={0.1} required value={Number((form.reliability_target * 100).toFixed(1))} onChange={(event) => set("reliability_target", event.target.valueAsNumber / 100)} /></Field>}
      </div>
      {form.mode === "demo" && <div className="notice">Demo trials use deterministic simulated outcomes.</div>}
      {needsProof && <div role="alert" className="notice error">Verify this site in the registry before starting a live run.</div>}
      <ErrorNotice error={create.error} />
      <div className="form-footer">
        <button type="button" className="button secondary" onClick={close} disabled={create.isPending}>Cancel</button>
        <button className="button primary" disabled={!form.target_id || !form.workflow_id || needsProof || create.isPending}><Play size={16} />{create.isPending ? "Starting…" : preview ? "Run preview" : "Start run"}</button>
      </div>
    </form>
  </dialog>;
}

export function RunRows({ runs, onOpen }: { runs: Run[]; onOpen: (id: string) => void }) {
  return <div className="table-scroll"><table>
    <thead><tr><th scope="col">Workflow</th><th scope="col">Site</th><th scope="col">Source</th><th scope="col">Status</th><th scope="col">Started</th><th scope="col"><span className="sr-only">Open run</span></th></tr></thead>
    <tbody>{runs.map((run) => <tr key={run.id}>
      <td><button type="button" className="table-link" onClick={() => onOpen(run.id)}>{run.workflow_name}</button><span className="table-subtitle">{label(run.phase)} · {run.repetitions} {run.repetitions === 1 ? "trial" : "trials"}</span></td>
      <td>{run.target_name}</td><td><Badge value={run.mode === "demo" ? "simulated" : "live"} /></td><td><Badge value={run.status} /></td>
      <td className="muted nowrap">{dateTime(run.created_at)}</td>
      <td><button type="button" className="icon-button bare" aria-label={`Open ${run.workflow_name} run`} onClick={() => onOpen(run.id)}><ArrowRight size={16} /></button></td>
    </tr>)}</tbody>
  </table></div>;
}

export function RunHistory({ runs, onOpen, onCreate }: { runs: Run[]; onOpen: (id: string) => void; onCreate: () => void }) {
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState("all");
  const [status, setStatus] = useState("all");
  const statuses = [...new Set(runs.map((run) => run.status))];
  const filtered = runs.filter((run) =>
    (mode === "all" || run.mode === mode) && (status === "all" || run.status === status) &&
    `${run.workflow_name} ${run.target_name}`.toLowerCase().includes(search.trim().toLowerCase()),
  );
  return <section className="panel data-panel">
    <SectionTitle title="Run history" detail={`${filtered.length} of ${runs.length} runs`} />
    <div className="filter-row">
      <input type="search" aria-label="Filter runs by name" placeholder="Search workflow or site…" value={search} onChange={(event) => setSearch(event.target.value)} />
      <Select aria-label="Evidence source" value={mode} onChange={(event) => setMode(event.target.value)}><option value="all">All sources</option><option value="live">Live browser</option><option value="demo">Simulated</option></Select>
      <Select aria-label="Run status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option>{statuses.map((entry) => <option key={entry} value={entry}>{label(entry)}</option>)}</Select>
    </div>
    {filtered.length ? <RunRows runs={filtered} onOpen={onOpen} /> : <Empty title={runs.length ? "No matching runs" : "No runs yet"} detail={runs.length ? "Change the filters to see other results." : "Start a run to capture trial outcomes and latency."} action={!runs.length && <button className="button primary" onClick={onCreate}><Play size={15} /> Workflow preview</button>} />}
  </section>;
}

const columns = createColumnHelper<Trial>();
const trialColumns = [
  columns.accessor("ordinal", { header: "Trial", cell: (info) => <span className="mono">{info.getValue()}</span> }),
  columns.accessor("status", { header: "State", cell: (info) => <Badge value={info.getValue()} /> }),
  columns.accessor("outcome", { header: "Outcome", cell: (info) => info.getValue() ? <Badge value={info.getValue()!} /> : "—" }),
  columns.accessor("elapsed_ms", { header: "Elapsed", cell: (info) => ms(info.getValue()) }),
  columns.accessor("attempts", { header: "Attempts" }),
  columns.accessor("error", { header: "Details", cell: (info) => info.getValue() || "—" }),
];

function TrialTable({ trials }: { trials: Trial[] }) {
  const [filter, setFilter] = useState("all");
  const data = useMemo(() => trials.filter((trial) => filter === "all" || trial.outcome === filter), [trials, filter]);
  // TanStack Table owns pagination state and exposes non-memoizable methods.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data,
    columns: trialColumns,
    getCoreRowModel: getCoreRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 12 } },
    autoResetPageIndex: false,
  });

  return <section className="panel data-panel">
    <SectionTitle title="Trials" detail={`${data.length} shown`} action={<Select aria-label="Filter by trial outcome" value={filter} onChange={(event) => { setFilter(event.target.value); table.setPageIndex(0); }}>
      <option value="all">All outcomes</option>{Array.from(new Set(trials.flatMap((trial) => trial.outcome ? [trial.outcome] : []))).map((entry) => <option key={entry} value={entry}>{label(entry)}</option>)}
    </Select>} />
    {!data.length ? <Empty title="No trial records" detail="Trial records appear after scheduling." /> : <>
      <div className="table-scroll"><table>
        <thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th scope="col" key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead>
        <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
      </table></div>
      <div className="pagination"><span>Page {table.getState().pagination.pageIndex + 1} of {table.getPageCount()}</span>
        <button className="icon-button" type="button" aria-label="Previous page" disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}><ChevronLeft size={16} /></button>
        <button className="icon-button" type="button" aria-label="Next page" disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}><ChevronRight size={16} /></button>
      </div>
    </>}
  </section>;
}

function latencyBuckets(trials: Trial[]) {
  const data = [
    { name: "≤ 250 ms", limit: 250, count: 0 },
    { name: "251–500", limit: 500, count: 0 },
    { name: "501–1k", limit: 1000, count: 0 },
    { name: "1k–2.5k", limit: 2500, count: 0 },
    { name: "> 2.5k", limit: Infinity, count: 0 },
  ];
  trials.forEach(({ elapsed_ms: time }) => {
    if (time === null) return;
    const bucket = data.find(({ limit }) => time <= limit);
    if (bucket) bucket.count += 1;
  });
  return data;
}

export function Metric({ title, value, detail, icon }: { title: string; value: string; detail: string; icon: React.ReactNode }) {
  return <div className="metric panel"><div className="metric-top"><span>{title}</span><span className="metric-icon">{icon}</span></div><strong>{value}</strong><small>{detail}</small></div>;
}

const segments = [
  { key: "completed", name: "Completed", color: "var(--cyan)" },
  { key: "failed", name: "Failed", color: "var(--rose)" },
  { key: "blocked", name: "Blocked", color: "var(--amber)" },
  { key: "cancelled", name: "Cancelled", color: "var(--faint)" },
  { key: "pending", name: "Pending", color: "var(--border-strong)" },
] as const;

function RunAnalytics({ report, trials }: { report: Report; trials: Trial[] }) {
  const { completion, counts, first_attempt: first, latency_ms: latency } = report;
  return <>
    <div className="metrics metrics-four">
      <Metric title="Workflow completion" value={pct(completion.rate)} detail={`${completion.successes} of ${completion.total} attempted`} icon={<Gauge size={17} />} />
      <Metric title="95% confidence interval" value={completion.low === null ? "—" : `${pct(completion.low)}–${pct(completion.high ?? null)}`} detail="Wilson score interval" icon={<ScanLine size={17} />} />
      <Metric title="First attempt success" value={pct(first.rate)} detail={`${first.successes} of ${first.total} attempted`} icon={<Gauge size={17} />} />
      <Metric title="Median latency" value={ms(latency.p50 ?? null)} detail={`p95 · ${ms(latency.p95 ?? null)}`} icon={<Clock3 size={17} />} />
    </div>
    <div className="two-up">
      <section className="panel chart-panel">
        <SectionTitle title="Trial outcomes" detail={`${counts.planned} planned`} />
        <div className="outcome-track" role="img" aria-label={segments.map(({ key, name }) => `${name} ${counts[key]}`).join(", ")}>
          {segments.map(({ key, color }) => <span key={key} style={{ flexGrow: counts[key], backgroundColor: color }} />)}
        </div>
        <ul className="outcome-legend">{segments.map(({ key, name, color }) => <li key={key}><span className="dot" style={{ backgroundColor: color }} />{name}<strong>{counts[key]}</strong></li>)}</ul>
      </section>
      <section className="panel chart-panel">
        <SectionTitle title="Execution latency" detail="Trial counts by elapsed time in milliseconds" />
        {latency.p50 == null ? <Empty title="No latency samples" detail="Finished trials contribute timing data." /> : <div className="latency-chart">
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
            <BarChart data={latencyBuckets(trials)} margin={{ top: 12, right: 4, bottom: 0, left: -22 }}>
              <CartesianGrid vertical={false} stroke="#263346" strokeDasharray="3 3" />
              <XAxis dataKey="name" tick={{ fill: "#a4b2c7", fontSize: 10 }} axisLine={false} tickLine={false} />
              <YAxis allowDecimals={false} tick={{ fill: "#a4b2c7", fontSize: 11 }} axisLine={false} tickLine={false} />
              <Tooltip cursor={{ fill: "#1c2d40" }} contentStyle={{ backgroundColor: "#131e2c", borderColor: "#304159", borderRadius: 8 }} />
              <Bar name="Trials" dataKey="count" fill="#57d5da" radius={[4, 4, 0, 0]} maxBarSize={34} />
            </BarChart>
          </ResponsiveContainer>
        </div>}
      </section>
    </div>
    <section className="panel conclusion">
      <div><Badge value={report.recommendation.status} /><h2>Workflow assessment</h2><p>{report.recommendation.message}</p></div>
      <div className="conclusion-side"><strong>Cost per completion</strong><span>{report.cost_per_success == null ? "Unknown" : new Intl.NumberFormat("en", { maximumFractionDigits: 2 }).format(report.cost_per_success)}</span></div>
      {report.limitations?.length ? <ul className="limitations">{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul> : null}
    </section>
  </>;
}

export function RunDetail({ run, token, onBack }: { run: Run; token: string; onBack: () => void }) {
  const query = useQueryClient();
  const [timeline, setTimeline] = useState<RunEvent[]>([]);
  const [format, setFormat] = useState<"json" | "csv" | "html" | "pdf">("json");
  const active = isActive(run.status);
  const trials = useQuery({
    queryKey: ["trials", token, run.id],
    queryFn: ({ signal }) => api<Trial[]>(`/runs/${run.id}/trials`, token, { signal }),
    refetchInterval: active ? 5000 : false,
  });
  const report = useQuery({
    queryKey: ["report", token, run.id],
    queryFn: ({ signal }) => api<Report>(`/runs/${run.id}/report`, token, { signal }),
    refetchInterval: active ? 5000 : false,
  });
  const control = useMutation({
    mutationFn: (action: "pause" | "resume" | "cancel") => api(`/runs/${run.id}/${action}`, token, { method: "POST" }),
    onSuccess: () => {
      void query.invalidateQueries({ queryKey: ["runs", token] });
      void query.invalidateQueries({ queryKey: ["report", token, run.id] });
      void query.invalidateQueries({ queryKey: ["trials", token, run.id] });
    },
  });
  const download = useMutation({ mutationFn: () => exportRun(run.id, format, token) });
  const archive = useMutation({
    mutationFn: () => api<{ bucket: string; key: string }>(`/runs/${run.id}/archive`, token, { method: "POST" }),
  });

  useEffect(() => {
    const controller = new AbortController();
    void streamRunEvents(run.id, token, controller.signal, (batch) => {
      setTimeline((events) => [...events, ...batch].slice(-12));
      void query.invalidateQueries({ queryKey: ["runs", token] });
      void query.invalidateQueries({ queryKey: ["trials", token, run.id] });
      void query.invalidateQueries({ queryKey: ["report", token, run.id] });
    }).catch(() => {
      // Keep polling active runs when the event stream is unavailable.
    });
    return () => controller.abort();
  }, [query, run.id, token]);

  useEffect(() => {
    if (!active) {
      void query.invalidateQueries({ queryKey: ["trials", token, run.id] });
      void query.invalidateQueries({ queryKey: ["report", token, run.id] });
    }
  }, [active, query, run.id, token]);

  return <div className="stack">
    <button className="back-link" onClick={onBack}><ArrowLeft size={16} /> Run history</button>
    <section className="panel run-heading">
      <div><div className="actions"><Badge value={run.mode === "demo" ? "simulated" : "live"} /><Badge value={run.status} /><ModeBadge mode={run.target_mode} /></div>
        <h2>{run.workflow_name}</h2>
        <p>{run.target_name} · {label(run.phase)} · {dateTime(run.created_at)}</p>
        <p className="run-metadata">Site v{run.target_version} · Workflow v{run.workflow_version} · Seed {run.seed}</p>
      </div>
      <div className="run-buttons">
        {active && <div className="actions">
          {run.status === "paused" ? <button className="button secondary" disabled={control.isPending} onClick={() => control.mutate("resume")}><Play size={16} /> Resume</button>
            : <button className="button secondary" disabled={control.isPending || run.status === "cancelling"} onClick={() => control.mutate("pause")}><Pause size={16} /> Pause</button>}
          <button className="button secondary danger" disabled={control.isPending || run.status === "cancelling"} onClick={() => control.mutate("cancel")}><CircleStop size={16} /> Cancel</button>
        </div>}
        <div className="actions"><Select aria-label="Export format" value={format} onChange={(event) => setFormat(event.target.value as typeof format)}>{["json", "csv", "html", "pdf"].map((option) => <option key={option} value={option}>{option.toUpperCase()}</option>)}</Select>
          <button className="button secondary" disabled={download.isPending || !report.data} onClick={() => download.mutate()}><ArrowDownToLine size={16} /> Export</button>
          {!active && <button className="button secondary" disabled={archive.isPending || !report.data} onClick={() => archive.mutate()}><Archive size={16} /> Archive</button>}
        </div>
      </div>
    </section>
    <ErrorNotice error={control.error || download.error || archive.error || report.error || trials.error} />
    {archive.data && <p className="notice" role="status">Report archived to <code>{archive.data.bucket}/{archive.data.key}</code>.</p>}
    {report.isPending || trials.isPending ? <Loading /> : null}
    {report.data && <RunAnalytics report={report.data} trials={trials.data ?? []} />}
    {trials.data && <TrialTable trials={trials.data} />}
    {timeline.length > 0 && <section className="panel chart-panel">
      <SectionTitle title="Recent run events" />
      <ol className="event-list">{[...timeline].reverse().map((event) =>
        <li key={event.id}><span className="dot" /><span>{label(event.kind)}</span><time dateTime={event.time}>{dateTime(event.time)}</time></li>,
      )}</ol>
    </section>}
  </div>;
}
