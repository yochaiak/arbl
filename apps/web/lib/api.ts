import { z } from "zod";
import type { components } from "./schema";

type Schemas = components["schemas"];

export const API_ROOT = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8400").replace(/\/$/, "");

export const actions = [
  "navigate", "click", "fill", "select", "extract", "wait_for", "assert_visible", "assert_text",
] as const satisfies readonly Schemas["Step"]["action"][];

export const actionLabels: Record<(typeof actions)[number], string> = {
  navigate: "Navigate",
  click: "Click",
  fill: "Fill field",
  select: "Select option",
  extract: "Extract text",
  wait_for: "Wait for element",
  assert_visible: "Assert visible",
  assert_text: "Assert text",
};

export const stepSchema = z.object({
  action: z.enum(actions),
  url: z.string().nullish(),
  selector: z.string().nullish(),
  value: z.string().nullish(),
  secret_ref: z.string().regex(/^[A-Za-z][A-Za-z0-9_]{0,47}$/).nullish(),
  timeout_ms: z.number().int().min(100).max(15000).optional(),
}).superRefine((step, ctx) => {
  if (step.action === "navigate" && (!step.url?.startsWith("/") || step.url.startsWith("//"))) {
    ctx.addIssue({ code: "custom", path: ["url"], message: "Enter a site-relative path beginning with /." });
  }
  if (step.action !== "navigate" && !step.selector?.trim()) {
    ctx.addIssue({ code: "custom", path: ["selector"], message: "Enter an element selector." });
  }
  if (["select", "assert_text"].includes(step.action) && !step.value?.trim()) {
    ctx.addIssue({ code: "custom", path: ["value"], message: "Enter a value." });
  }
  if (step.action === "fill" && (step.value == null) === !step.secret_ref) {
    ctx.addIssue({ code: "custom", path: ["value"], message: "Enter one value or secret reference." });
  }
});

export const workflowSchema = z.object({
  name: z.string().trim().min(1, "Enter a workflow name.").max(100),
  steps: z.array(stepSchema).min(2, "Add at least two steps.").max(40),
}).superRefine((workflow, ctx) => {
  if (workflow.steps[0]?.action !== "navigate") {
    ctx.addIssue({ code: "custom", path: ["steps", 0], message: "Start with a Navigate step." });
  }
  const last = workflow.steps.at(-1)?.action;
  if (last !== "assert_visible" && last !== "assert_text") {
    ctx.addIssue({ code: "custom", path: ["steps"], message: "Finish with a success assertion." });
  }
});

export type Step = Omit<Schemas["Step"], "timeout_ms"> & { timeout_ms?: number };
export type WorkflowInput = Omit<Schemas["WorkflowInput"], "steps"> & { steps: Step[] };
export type Workflow = Schemas["WorkflowView"];

export type TargetInput = Omit<Schemas["TargetInput"], "authorized" | "resource_origins"> & {
  authorized: boolean;
  resource_origins: string[];
};
export type Target = Schemas["TargetView"];

export type RunInput = Omit<Schemas["RunInput"], "repetitions"> & { repetitions: number };
export type Run = Schemas["RunView"];
export type Trial = Schemas["TrialView"];
export type Report = Schemas["ReportView"];

export interface SystemStatus {
  browser: { available: boolean; reason?: string };
  worker: { ready: boolean };
  adscore: { configured: boolean; ready: boolean; reason?: string };
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

function errorDetail(payload: unknown): string | undefined {
  if (typeof payload !== "object" || payload === null || !("detail" in payload)) return;
  const { detail } = payload;
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return;
  return detail.map((item: { loc?: unknown[]; msg?: string }) => {
    const field = item.loc?.filter((part) => part !== "body").join(".");
    return [field, item.msg].filter(Boolean).join(": ");
  }).join("; ");
}

async function request(path: string, token: string, options?: { method?: string; body?: unknown; signal?: AbortSignal }) {
  const response = await fetch(`${API_ROOT}/api/v1${path}`, {
    method: options?.method ?? "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      ...(options?.body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: options?.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options?.signal,
    cache: "no-store",
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    throw new ApiError(errorDetail(payload) || `The API returned HTTP ${response.status}.`, response.status);
  }
  return response;
}

export async function api<T>(path: string, token: string, options?: Parameters<typeof request>[2]): Promise<T> {
  const response = await request(path, token, options);
  if (response.status === 204) return undefined as T;
  return await response.json() as T;
}

export function saveFile(contents: Blob, filename: string) {
  const url = URL.createObjectURL(contents);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface RunEvent {
  id: string;
  kind: string;
  time: string;
}

export async function streamRunEvents(id: string, token: string, signal: AbortSignal, onBatch: (events: RunEvent[]) => void) {
  const response = await request(`/runs/${encodeURIComponent(id)}/events`, token, { signal });
  if (!response.body) return;
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    for (;;) {
      const result = await reader.read();
      if (result.done) return;
      buffer += result.value;
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      const events: RunEvent[] = [];
      for (const frame of frames) {
        const lines = frame.split("\n");
        const eventId = lines.find((line) => line.startsWith("id: "))?.slice(4);
        const payload = lines.find((line) => line.startsWith("data: "))?.slice(6);
        if (!eventId || !payload) continue;
        const parsed: unknown = JSON.parse(payload);
        if (typeof parsed === "object" && parsed && "kind" in parsed && "time" in parsed
            && typeof parsed.kind === "string" && typeof parsed.time === "string") {
          events.push({ id: eventId, kind: parsed.kind, time: parsed.time });
        }
      }
      if (events.length) onBatch(events);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export async function exportRun(id: string, format: "json" | "csv" | "html" | "pdf", token: string) {
  const response = await request(`/runs/${encodeURIComponent(id)}/export?format=${format}`, token);
  saveFile(await response.blob(), `abrl-${id}.${format}`);
}

export const label = (value: string) => value.replaceAll("_", " ");
export const pct = (value: number | null) =>
  value === null ? "—" : new Intl.NumberFormat("en", { style: "percent", maximumFractionDigits: 1 }).format(value);
export const ms = (value: number | null) => value === null ? "—" : `${Math.round(value).toLocaleString()} ms`;
export const dateTime = (value: string) => new Intl.DateTimeFormat("en", {
  month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
}).format(new Date(value));
export const isActive = (status: string) => ["queued", "pending", "running", "paused", "cancelling"].includes(status.toLowerCase());
