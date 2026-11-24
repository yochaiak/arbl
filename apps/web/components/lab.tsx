"use client";

import { useState, useSyncExternalStore, type FormEvent } from "react";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  CheckCircle2,
  CircleDot,
  Globe2,
  LayoutGrid,
  ListChecks,
  LockKeyhole,
  LogOut,
  Plus,
  Radar,
  Route,
  Terminal,
  Video,
  CalendarClock,
  Monitor,
  Network,
  Server,
  Workflow as WorkflowIcon,
} from "lucide-react";
import {
  API_ROOT,
  PROJECT_KEY,
  currentProject,
  ApiError,
  api,
  dateTime,
  isActive,
  type Run,
  type SystemStatus,
  type Target,
  type Workflow,
} from "@/lib/api";
import { SiteRegistry, WorkflowRegistry } from "./registry";
import { Metric, RunDetail, RunHistory, RunRows } from "./runs";
import {
  Badge,
  Brand,
  Empty,
  ErrorNotice,
  Loading,
  SectionTitle,
  Select,
} from "./ui";
import { BatchWizard, Sessions } from "./sessions";
import { Schedules } from "./schedules";
import { NetworkPage } from "./network";
import { FleetPage } from "./fleet";
import {
  type Project,
  type Batch,
  type PageResult,
  type Schedule,
  platformKey,
} from "@/lib/platform";

const pages = [
  {
    key: "overview",
    title: "Overview",
    icon: LayoutGrid,
    detail: "Monitor your sites, workflows, and recent trials.",
  },
  {
    key: "sessions",
    title: "Sessions",
    icon: Monitor,
    detail:
      "Independent browsers, durable profiles, and controlled manual access.",
  },
  {
    key: "sites",
    title: "Sites",
    icon: Globe2,
    detail: "Register and verify the sites available for browser runs.",
  },
  {
    key: "workflows",
    title: "Workflows",
    icon: Route,
    detail: "Build and version workflows with explicit success checks.",
  },
  {
    key: "runs",
    title: "Runs",
    icon: Activity,
    detail: "Historical workflow runs and their outcomes.",
  },
  {
    key: "schedules",
    title: "Schedules",
    icon: CalendarClock,
    detail: "Run saved workflows on a dependable, timezone-aware schedule.",
  },
  {
    key: "network",
    title: "Network",
    icon: Network,
    detail:
      "Policy-driven proxy selection, transport health, and connection continuity.",
  },
  {
    key: "fleet",
    title: "Fleet & settings",
    icon: Server,
    detail: "Runtime capacity, browser integrations, and project access.",
  },
  {
    key: "system",
    title: "Diagnostics",
    icon: Radar,
    detail: "Check runtime, verification, and recording availability.",
  },
] as const;
type Page = (typeof pages)[number]["key"];

const TOKEN_KEY = "abrl.operator";
const TOKEN_EVENT = "abrl:operator";

function subscribeToken(callback: () => void) {
  window.addEventListener(TOKEN_EVENT, callback);
  return () => window.removeEventListener(TOKEN_EVENT, callback);
}

function changeToken(value: string) {
  if (value) window.sessionStorage.setItem(TOKEN_KEY, value);
  else window.sessionStorage.removeItem(TOKEN_KEY);
  window.dispatchEvent(new Event(TOKEN_EVENT));
}

const subscribeHydration = () => () => {};

function TokenForm({ disconnected = false }: { disconnected?: boolean }) {
  const [draft, setDraft] = useState("");
  const hydrated = useSyncExternalStore(
    subscribeHydration,
    () => true,
    () => false,
  );
  const connect = useMutation({
    mutationFn: async (token: string) => {
      try {
        await api<SystemStatus>("/status", token);
      } catch (error) {
        if (error instanceof ApiError && [401, 403].includes(error.status)) {
          throw new Error(
            "The operator token was rejected. Check your local configuration.",
          );
        }
        if (error instanceof TypeError)
          throw new Error(
            `Could not reach the API at ${API_ROOT}. Start the local services and try again.`,
          );
        throw error;
      }
      return token;
    },
    onSuccess: changeToken,
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (draft.trim()) connect.mutate(draft.trim());
  }

  return (
    <div className="auth-shell">
      <div className="auth-header">
        <Brand />
        <span className="environment">
          <span className="dot" /> Local workspace
        </span>
      </div>
      <main className="auth-grid">
        <div className="auth-copy">
          <div className="eyebrow">
            <Terminal size={15} /> ABRL WORKSPACE
          </div>
          <h1>
            Browser workflow
            <br />
            operations.
          </h1>
          <p>
            Define repeatable workflows, run them on registered sites, and
            review completion, timing, and failure evidence.
          </p>
          <div className="auth-features">
            <span>
              <Globe2 size={16} /> Site registry
            </span>
            <span>
              <WorkflowIcon size={16} /> Workflow builder
            </span>
            <span>
              <Activity size={16} /> Session results
            </span>
          </div>
        </div>
        <form onSubmit={submit} className="panel auth-form">
          <span className="auth-icon">
            <LockKeyhole size={22} />
          </span>
          <h2>Connect to ABRL</h2>
          <p>Enter the operator token from your local setup.</p>
          {disconnected && (
            <p className="notice">
              Your saved token was rejected. Enter the current token.
            </p>
          )}
          <label className="field">
            <span>Operator token</span>
            <input
              required
              disabled={!hydrated}
              autoComplete="off"
              type="password"
              value={draft}
              placeholder="Enter operator token"
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <ErrorNotice error={connect.error} />
          <button
            disabled={connect.isPending || !draft.trim()}
            className="button primary"
          >
            {connect.isPending ? "Connecting…" : "Connect"}
            <ArrowRight size={16} />
          </button>
          <a className="button secondary" href={`${API_ROOT}/auth/login`}>
            Sign in with your organization <ArrowUpRight size={15} />
          </a>
          <small className="mono muted">{API_ROOT}</small>
        </form>
      </main>
    </div>
  );
}

function Overview({
  token,
  targets,
  workflows,
  runs,
  onOpen,
  onCreate,
  onPreview,
  onNavigate,
}: {
  token: string;
  targets: Target[];
  workflows: Workflow[];
  runs: Run[];
  onOpen: (id: string) => void;
  onCreate: () => void;
  onPreview: () => void;
  onNavigate: (page: Page) => void;
}) {
  const active = runs.filter((run) => isActive(run.status)).length;
  return (
    <div className="stack">
      <section className="panel overview-hero">
        <div className="hero-copy">
          <span className="eyebrow">
            <span className="dot" /> BROWSER OPERATIONS
          </span>
          <h2>
            Run workflows
            <br className="hero-break" /> in live browsers.
          </h2>
          <p>
            Choose a site and a workflow, then review session outcomes and retained artifacts.
          </p>
          <div className="actions">
            <button className="button primary" onClick={onCreate}>
              <Plus size={17} /> Create session batch
            </button>
            <button className="button secondary" onClick={onPreview}>
              <Route size={16} /> Workflow preview
            </button>
          </div>
        </div>
        <div className="hero-visual" aria-hidden="true">
          <div className="hero-orbit orbit-outer" />
          <div className="hero-orbit orbit-inner" />
          <div className="hero-node node-top">
            <Globe2 size={19} />
          </div>
          <div className="hero-node node-bottom">
            <ListChecks size={19} />
          </div>
          <div className="hero-node node-center">
            <Activity size={46} strokeWidth={1.45} />
          </div>
        </div>
      </section>
      <OperationalSummary token={token} onNavigate={onNavigate} />
      <div className="metrics metrics-four">
        <Metric
          title="Registered sites"
          value={String(targets.length)}
          detail={`${targets.filter(({ verified }) => verified).length} verified`}
          icon={<Globe2 size={17} />}
        />
        <Metric
          title="Saved workflows"
          value={String(workflows.length)}
          detail="Latest versions"
          icon={<Route size={17} />}
        />
        <Metric
          title="Active runs"
          value={String(active)}
          detail="Queued, running, or paused"
          icon={<Activity size={17} />}
        />
        <Metric
          title="Completed runs"
          value={String(
            runs.filter(({ status }) => status === "completed").length,
          )}
          detail={`${runs.length} total runs`}
          icon={<CheckCircle2 size={17} />}
        />
      </div>
      <section className="panel data-panel">
        <SectionTitle
          title="Recent runs"
          detail="Results from earlier workflow runs."
        />
        {runs.length ? (
          <RunRows runs={runs.slice(0, 6)} onOpen={onOpen} />
        ) : (
          <Empty
            title="No trial data yet"
            detail="New workflow previews appear in Sessions."
          />
        )}
      </section>
    </div>
  );
}

function OperationalSummary({
  token,
  onNavigate,
}: {
  token: string;
  onNavigate: (page: Page) => void;
}) {
  const fleet = useQuery({
    queryKey: platformKey(token, "fleet"),
    queryFn: () =>
      api<{
        active: number;
        queued: number;
        admitted: number;
        global_limit: number;
      }>("/fleet", token),
    refetchInterval: 5000,
  });
  const batches = useQuery({
    queryKey: platformKey(token, "overview-batches"),
    queryFn: () => api<PageResult<Batch>>("/batches?limit=4", token),
    refetchInterval: 5000,
  });
  const schedules = useQuery({
    queryKey: platformKey(token, "overview-schedules"),
    queryFn: () => api<PageResult<Schedule>>("/schedules?limit=4", token),
    refetchInterval: 10000,
  });
  return (
    <div className="operations-overview">
      <section className="panel operations-card">
        <div className="subsection-header">
          <span className="eyebrow">SESSION OPERATIONS</span>
          <Monitor size={18} />
        </div>
        <div className="operations-count">
          <strong>{fleet.data?.active ?? "—"}</strong>
          <span>
            active browsers
            <small>
              {fleet.data?.queued ?? "—"} queued ·{" "}
              {fleet.data?.global_limit ?? 500} global limit
            </small>
          </span>
        </div>
        <button className="text-button" onClick={() => onNavigate("sessions")}>
          Manage sessions <ArrowRight size={15} />
        </button>
        <ErrorNotice error={fleet.error} />
      </section>
      <section className="panel operations-card">
        <div className="subsection-header">
          <span className="eyebrow">RECENT BATCHES</span>
          <button
            className="text-button"
            onClick={() => onNavigate("sessions")}
          >
            View all <ArrowRight size={14} />
          </button>
        </div>
        {batches.data?.items.length ? (
          <ul className="compact-records">
            {batches.data.items.map((batch) => (
              <li key={batch.id}>
                <span>
                  {batch.name}
                  <small>{batch.count} sessions</small>
                </span>
                <Badge value={batch.status} />
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            No session batches yet. Create independent browsers from one saved
            workflow.
          </p>
        )}
        <ErrorNotice error={batches.error} />
      </section>
      <section className="panel operations-card">
        <div className="subsection-header">
          <span className="eyebrow">SCHEDULED WORK</span>
          <button
            className="text-button"
            onClick={() => onNavigate("schedules")}
          >
            View all <ArrowRight size={14} />
          </button>
        </div>
        {schedules.data?.items.length ? (
          <ul className="compact-records">
            {schedules.data.items.map((schedule) => (
              <li key={schedule.id}>
                <span>
                  {schedule.name}
                  <small>{schedule.timezone}</small>
                </span>
                <span className="small-text muted">
                  {schedule.enabled === false
                    ? "Paused"
                    : schedule.next_fire_at
                      ? dateTime(schedule.next_fire_at)
                      : "—"}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            No scheduled work. Define an interval or calendar trigger with
            explicit overlap rules.
          </p>
        )}
        <ErrorNotice error={schedules.error} />
      </section>
    </div>
  );
}

function Diagnostics({ status }: { status?: SystemStatus }) {
  const recording = status?.recording;
  return (
    <div className="stack">
      <div className="metrics metrics-four">
        <Metric
          title="Standard Chromium"
          value={status?.browser.available ? "Available" : "Unavailable"}
          detail={status?.browser.reason || "Playwright runtime"}
          icon={<Globe2 size={17} />}
        />
        <Metric
          title="Trial worker"
          value={status?.worker.ready ? "Ready" : "Offline"}
          detail="Local scheduler"
          icon={<Activity size={17} />}
        />
        <Metric
          title="AdScore verification"
          value={
            status?.adscore.ready
              ? "Verified"
              : status?.adscore.configured
                ? "Configured"
                : "Unconfigured"
          }
          detail={status?.adscore.reason || "Site observation connector"}
          icon={<Radar size={17} />}
        />
        <Metric
          title="Recording encoder"
          value={recording?.reporting_workers ? (recording.ready_workers ? "Available" : "Unavailable") : "Unreported"}
          detail={recording?.reporting_workers
            ? `${recording.ready_workers} of ${recording.reporting_workers} workers ready`
            : "Waiting for a session worker"}
          icon={<Video size={17} />}
        />
      </div>
    </div>
  );
}

function Workspace({ token }: { token: string }) {
  const [page, setPage] = useState<Page>(() => {
    const desired =
      typeof window !== "undefined"
        ? new URLSearchParams(window.location.search).get("view")
        : null;
    return pages.some((p) => p.key === desired)
      ? (desired as Page)
      : "overview";
  });
  const [runId, setRunId] = useState<string | null>(null);
  const [batchLaunch, setBatchLaunch] = useState<{ preview?: Workflow | null } | null>(null);
  const query = useQueryClient();
  const sites = useQuery({
    queryKey: ["targets", token],
    queryFn: ({ signal }) => api<Target[]>("/targets", token, { signal }),
  });
  const workflows = useQuery({
    queryKey: ["workflows", token],
    queryFn: ({ signal }) => api<Workflow[]>("/workflows", token, { signal }),
  });
  const runs = useQuery({
    queryKey: ["runs", token],
    queryFn: ({ signal }) => api<Run[]>("/runs", token, { signal }),
    refetchInterval: ({ state }) =>
      state.data?.some(({ status }) => isActive(status)) ? 1000 : false,
  });
  const status = useQuery({
    queryKey: ["status", token],
    queryFn: ({ signal }) => api<SystemStatus>("/status", token, { signal }),
    refetchInterval: 10000,
  });
  const projects = useQuery({
    queryKey: platformKey(token, "projects"),
    queryFn: () => api<Project[]>("/projects", token),
  });
  const project = projects.data?.find((item) => item.id === currentProject());
  const current = pages.find(({ key }) => key === page)!;
  const selectedRun = runs.data?.find(({ id }) => id === runId);
  const errors = [
    sites.error,
    workflows.error,
    runs.error,
    status.error,
  ].filter(Boolean);
  if (errors.some((error) => error instanceof ApiError && error.status === 401))
    return <TokenForm disconnected />;

  function navigate(next: Page) {
    setPage(next);
    setRunId(null);
    window.history.replaceState(null, "", `?view=${next}`);
  }

  function openRun(id: string) {
    setRunId(id);
    setPage("runs");
  }

  async function disconnect() {
    await fetch(`${API_ROOT}/auth/logout`, {
      method: "POST",
      credentials: "include",
    }).catch(() => {});
    query.clear();
    changeToken("");
  }

  const openPreview = () => setBatchLaunch({ preview: workflows.data?.[0] ?? null });

  return (
    <div className="workspace">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className="sidebar">
        <Brand />
        <div className="project-switcher">
          <span>WORKSPACE</span>
          <Select
            aria-label="Project"
            value={currentProject()}
            onChange={(event) => {
              window.sessionStorage.setItem(PROJECT_KEY, event.target.value);
              query.clear();
              window.dispatchEvent(new Event(TOKEN_EVENT));
            }}
          >
            {projects.data?.length ? (
              projects.data.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))
            ) : (
              <option value="default">Local project</option>
            )}
          </Select>
          {project && <small>{project.role} access</small>}
        </div>
        <nav aria-label="Workspace">
          {pages.map(({ key, title, icon: Icon }) => (
            <button
              key={key}
              type="button"
              aria-current={page === key ? "page" : undefined}
              className={`nav-item ${page === key ? "active" : ""}`}
              onClick={() => navigate(key)}
            >
              <Icon size={18} />
              <span>{title}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="worker-state">
            <CircleDot
              size={15}
              className={status.data?.worker.ready ? "text-cyan" : "muted"}
            />
            <div>
              <strong>Local runtime</strong>
              <small>
                {status.data?.worker.ready
                  ? "Worker ready"
                  : "Worker unavailable"}
              </small>
            </div>
          </div>
          <button className="nav-item disconnect" onClick={disconnect}>
            <LogOut size={17} />
            <span>Disconnect</span>
          </button>
        </div>
      </aside>
      <main id="main-content" className="main">
        <div className="main-inner">
          <header className="page-header">
            <div>
              <h1>{current.title}</h1>
              <p>{current.detail}</p>
            </div>
            {["sites", "workflows", "runs"].includes(page) && (
              <button className="button secondary" onClick={openPreview}>
                <Route size={17} /> Workflow preview
              </button>
            )}
          </header>
          {errors[0] && (
            <ErrorNotice
              error={
                errors[0] instanceof TypeError
                  ? new Error(`Could not reach the API at ${API_ROOT}.`)
                  : errors[0]
              }
            />
          )}
          {sites.isPending || workflows.isPending || runs.isPending ? (
            <Loading />
          ) : (
            <>
              {page === "overview" && (
                <Overview
                  token={token}
                  targets={sites.data ?? []}
                  workflows={workflows.data ?? []}
                  runs={runs.data ?? []}
                  onOpen={openRun}
                  onCreate={() => setBatchLaunch({})}
                  onPreview={openPreview}
                  onNavigate={navigate}
                />
              )}
              {page === "sites" && (
                <SiteRegistry token={token} targets={sites.data ?? []} />
              )}
              {page === "workflows" && (
                <WorkflowRegistry
                  token={token}
                  workflows={workflows.data ?? []}
                  onPreview={(workflow) => setBatchLaunch({ preview: workflow })}
                />
              )}
              {page === "runs" &&
                (selectedRun ? (
                  <RunDetail
                    key={selectedRun.id}
                    token={token}
                    run={selectedRun}
                    onBack={() => setRunId(null)}
                  />
                ) : (
                  <RunHistory
                    runs={runs.data ?? []}
                    onOpen={openRun}
                    onCreate={openPreview}
                  />
                ))}
              {page === "sessions" && (
                <Sessions
                  token={token}
                  targets={sites.data ?? []}
                  workflows={workflows.data ?? []}
                />
              )}
              {page === "schedules" && (
                <Schedules
                  token={token}
                  targets={sites.data ?? []}
                  workflows={workflows.data ?? []}
                />
              )}
              {page === "network" && <NetworkPage token={token} />}
              {page === "fleet" && <FleetPage token={token} />}
              {page === "system" && (
                <Diagnostics status={status.data} />
              )}
            </>
          )}
        </div>
      </main>
      {batchLaunch && (
        <BatchWizard
          token={token}
          targets={sites.data ?? []}
          workflows={workflows.data ?? []}
          preview={batchLaunch.preview}
          close={() => setBatchLaunch(null)}
          onCreated={() => navigate("sessions")}
        />
      )}
    </div>
  );
}

function Session() {
  const token = useSyncExternalStore(
    subscribeToken,
    () => window.sessionStorage.getItem(TOKEN_KEY) || "",
    () => "",
  );
  const project = useSyncExternalStore(
    subscribeToken,
    currentProject,
    () => "default",
  );
  const identity = useQuery({
    queryKey: ["identity", token],
    queryFn: () => api<{ user_id: string }>("/me", token),
    retry: false,
    enabled: !token,
  });
  return token || identity.data ? (
    <Workspace key={`${token}-${project}`} token={token} />
  ) : (
    <TokenForm />
  );
}

export function BrowserWorkspace() {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, staleTime: 1000 },
          mutations: { retry: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <Session />
    </QueryClientProvider>
  );
}
