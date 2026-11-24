import { z } from "zod";
import type { components } from "./schema";

type Schemas = components["schemas"];

export const fixtureOrigin = "http://127.0.0.1:8400";
export const API_ROOT = (
  process.env.NEXT_PUBLIC_API_URL || fixtureOrigin
).replace(/\/$/, "");

export const legacyActions = [
  "navigate",
  "click",
  "fill",
  "select",
  "extract",
  "wait_for",
  "assert_visible",
  "assert_text",
  "assert_not_visible",
  "checkpoint",
] as const satisfies readonly Schemas["WorkflowInput"]["steps"][number]["action"][];

export const actions = [
  ...legacyActions,
  "check",
  "uncheck",
  "press",
  "hover",
  "wait_for_text",
  "wait_for_hidden",
  "wait_for_url",
  "assert_url",
  "if",
  "repeat",
  "use_block",
  "open_tab",
  "switch_tab",
  "close_tab",
  "expect_popup",
] as const;

export const actionLabels: Record<(typeof actions)[number], string> = {
  navigate: "Navigate",
  click: "Click",
  fill: "Fill field",
  select: "Select option",
  extract: "Extract text",
  wait_for: "Wait for element",
  assert_visible: "Assert visible",
  assert_text: "Assert text",
  assert_not_visible: "Assert not visible · failure guard",
  checkpoint: "Record checkpoint",
  check: "Check checkbox",
  uncheck: "Uncheck checkbox",
  press: "Press key",
  hover: "Hover",
  wait_for_text: "Wait for text",
  wait_for_hidden: "Wait for element to hide",
  wait_for_url: "Wait for URL",
  assert_url: "Assert URL",
  if: "If condition",
  repeat: "Repeat steps",
  use_block: "Use reusable block",
  open_tab: "Open tab",
  switch_tab: "Switch tab",
  close_tab: "Close tab",
  expect_popup: "Click and expect popup",
};

const fixturePlaceholder = /\[([A-Za-z][A-Za-z0-9_]*)\]/;
const fixtureParameterName = /^[A-Za-z][A-Za-z0-9_]{0,31}$/;
const workflowParameterLimit = 16;
const navigationUrlMaxLength = 1500;
const fixturePaths = new Set([
  "/lab",
  "/lab/",
  "/lab/interaction",
  "/lab/form",
  "/lab/next",
  "/lab/finish",
  "/lab/repeat",
  "/lab/research",
]);
export const fixtureParameterMaxLength = 200;
export const fixtureParameterValue = z
  .string()
  .refine((value) => !/\p{Cc}/u.test(value), "Remove control characters from the URL parameter value.")
  .transform((value) => value.replace(/^\p{White_Space}+|\p{White_Space}+$/gu, ""))
  .refine((value) => value.length > 0, "Enter a value for each URL parameter.")
  .refine(
    (value) => Array.from(value).length <= fixtureParameterMaxLength,
    `Use at most ${fixtureParameterMaxLength} characters per URL parameter value.`,
  );

function fixturePath(url: string | null | undefined): string | null {
  if (!url || /[ \\\p{Cc}]/u.test(url)) return null;
  const relative = url.startsWith(`${fixtureOrigin}/`)
    ? url.slice(fixtureOrigin.length)
    : url;
  const [path] = relative.split(/[?#]/u, 1);
  return fixturePaths.has(path) ? path : null;
}

function navigationQuery(url: string) {
  const separator = url.indexOf("?");
  return separator < 0 ? { base: url, parts: [] } : {
    base: url.slice(0, separator),
    parts: url.slice(separator + 1).split("&").map((raw) => {
      const equals = raw.indexOf("=");
      const key = equals < 0 ? raw : raw.slice(0, equals);
      const value = equals < 0 ? "" : raw.slice(equals + 1);
      return { raw, key, value };
    }),
  };
}

function decodeUrlText(value: string, query = false) {
  return decodeURIComponent((query ? value.replaceAll("+", " ") : value).replace(/%(?![\da-f]{2})/gi, "%25"));
}

function urlPlaceholderName(value: string) {
  const match = fixturePlaceholder.exec(value);
  return match?.[0] === value && fixtureParameterName.test(match[1]) ? match[1] : undefined;
}

export function workflowFixtureParameters(url: string | null | undefined): string[] {
  if (!url || url.includes("#")) return [];
  const names = new Set<string>();
  for (const { value } of navigationQuery(url).parts) {
    try {
      const name = urlPlaceholderName(decodeUrlText(value, true));
      if (name) names.add(name);
    } catch {
      // The full template validator reports malformed URL encoding.
    }
  }
  return [...names];
}

type ParameterizedStep = {
  action: (typeof actions)[number];
  url?: string | null;
  fixture_params?: Record<string, string | undefined> | null;
  steps?: ParameterizedStep[] | null;
  else_steps?: ParameterizedStep[] | null;
};

export function workflowStepsWithPaths(
  steps: readonly ParameterizedStep[],
  prefix = "",
): { step: ParameterizedStep; path: string }[] {
  return steps.flatMap((step, index) => {
    const path = `${prefix}${index + 1}`;
    return [
      { step, path },
      ...workflowStepsWithPaths(step.steps ?? [], `${path}.`),
      ...workflowStepsWithPaths(step.else_steps ?? [], `${path}.else.`),
    ];
  });
}

export function workflowUrlParameterNames(steps: readonly ParameterizedStep[]): string[] {
  const names = new Set<string>();
  for (const { step } of workflowStepsWithPaths(steps)) {
    if (!step.fixture_params) continue;
    for (const name of workflowFixtureParameters(step.url)) {
      if (Object.hasOwn(step.fixture_params, name)) names.add(name);
    }
  }
  return [...names];
}

function encodeQueryValue(value: string) {
  return encodeURIComponent(value)
    .replace(/[!'()*]/g, (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`)
    .replaceAll("%20", "+");
}

export function workflowUrlPreview(
  url: string | null | undefined,
  values: Record<string, string | undefined>,
): { url: string; error?: never } | { url?: never; error: string } {
  if (!url || url.includes("#")) return { error: "Use a navigation URL without a fragment." };
  if (Array.from(url).length > navigationUrlMaxLength || /[ \\\p{Cc}]/u.test(url)) {
    return { error: `Use a URL of up to ${navigationUrlMaxLength} characters without spaces, backslashes, or control characters.` };
  }
  const wholeValueError = "Use URL parameter names of up to 32 letters, digits, or underscores as complete query values.";
  const { base, parts } = navigationQuery(url);
  const names = new Set<string>();
  const encoded = [];
  try {
    if (fixturePlaceholder.test(decodeUrlText(base))) return { error: wholeValueError };
    for (const part of parts) {
      const key = decodeUrlText(part.key, true);
      const value = decodeUrlText(part.value, true);
      if (/\p{Cc}/u.test(key) || /\p{Cc}/u.test(value)) {
        return { error: "Remove control characters from the URL query." };
      }
      if (fixturePlaceholder.test(key)) return { error: wholeValueError };
      if (!fixturePlaceholder.test(value)) {
        encoded.push(part.raw);
        continue;
      }
      const name = urlPlaceholderName(value);
      if (!name) return { error: wholeValueError };
      names.add(name);
      if (!Object.hasOwn(values, name) || values[name] == null) {
        return { error: `Enter a value for ${name}.` };
      }
      const parsed = fixtureParameterValue.safeParse(values[name]);
      if (!parsed.success) return { error: `${name}. ${parsed.error.issues[0].message}` };
      encoded.push(`${part.key}=${encodeQueryValue(parsed.data)}`);
    }
  } catch {
    return { error: "Use valid UTF-8 in the URL and parameter values." };
  }
  if (!names.size || Object.keys(values).some((name) => !names.has(name))) {
    return { error: "Supply values only for placeholders used in the URL." };
  }
  const resolved = `${base}?${encoded.join("&")}`;
  if (Array.from(resolved).length > navigationUrlMaxLength) {
    return { error: `The resolved URL exceeds ${navigationUrlMaxLength} characters.` };
  }
  return { url: resolved };
}

export function workflowUrlPreviews(steps: readonly ParameterizedStep[], overrides: Record<string, string>) {
  return workflowStepsWithPaths(steps).flatMap(({ step, path }) => {
    if (!step.fixture_params) return [];
    const values = Object.fromEntries(
      Object.entries(step.fixture_params).map(([name, value]) => [
        name, Object.hasOwn(overrides, name) ? overrides[name] : value,
      ]),
    );
    return [{ step: path, defaults: step.fixture_params, ...workflowUrlPreview(step.url, values) }];
  });
}

export function validateWorkflowUrlOverrides(
  steps: readonly ParameterizedStep[],
  draft: Record<string, string>,
): { overrides: Record<string, string>; error?: string } {
  const supported = new Set(workflowUrlParameterNames(steps));
  const entries: [string, string][] = [];
  for (const [name, value] of Object.entries(draft)) {
    if (!supported.has(name)) return { overrides: {}, error: `This workflow does not use ${name}.` };
    if (/^\p{White_Space}*$/u.test(value) && !/\p{Cc}/u.test(value)) continue;
    const parsed = fixtureParameterValue.safeParse(value);
    if (!parsed.success) return { overrides: {}, error: `${name}. ${parsed.error.issues[0].message}` };
    entries.push([name, parsed.data]);
  }
  const overrides = Object.fromEntries(entries);
  const invalid = workflowUrlPreviews(steps, overrides).find((preview) => preview.error);
  return { overrides, error: invalid ? `Step ${invalid.step}. ${invalid.error}` : undefined };
}

export const postNavigationWaitMaxMs = 60_000;
export const navigationTimeoutMaxMs = 120_000;
const initialNavigationTimeoutSchema = z.number().int().min(100).max(navigationTimeoutMaxMs).nullish();

export const stepSchema = z
  .object({
    action: z.enum(legacyActions),
    url: z.string().nullish(),
    fixture_params: z.record(z.string().regex(fixtureParameterName), fixtureParameterValue).nullish(),
    selector: z.string().nullish(),
    name: z.string().regex(/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/).nullish(),
    value: z.string().nullish(),
    secret_ref: z
      .string()
      .regex(/^[A-Za-z][A-Za-z0-9_]{0,47}$/)
      .nullish(),
    timeout_ms: initialNavigationTimeoutSchema,
    post_navigation_wait_ms: z.number().int().min(0).max(postNavigationWaitMaxMs).nullish(),
  })
  .superRefine((step, ctx) => {
    if (step.action !== "navigate" && step.timeout_ms != null && step.timeout_ms > 15_000) {
      ctx.addIssue({
        code: "custom",
        path: ["timeout_ms"],
        message: "Version 1 non-navigation step timeouts cannot exceed 15000 milliseconds.",
      });
    }
    let validNavigation =
      Boolean(step.url?.startsWith("/") && !step.url.startsWith("//")) ||
      fixturePath(step.url) != null;
    if (step.url?.startsWith("https://")) {
      try {
        const url = new URL(step.url);
        validNavigation = !url.username && !url.password;
      } catch {
        validNavigation = false;
      }
    }
    if (step.action === "navigate" && !validNavigation) {
      ctx.addIssue({
        code: "custom",
        path: ["url"],
        message: "Enter a site-relative path, a local ABRL test URL, or an approved HTTPS URL.",
      });
    }
    if (step.action !== "navigate" && step.post_navigation_wait_ms != null) {
      ctx.addIssue({
        code: "custom",
        path: ["post_navigation_wait_ms"],
        message: "Only Navigate steps support a wait after navigation.",
      });
    }
    if (step.fixture_params != null) {
      if (step.action !== "navigate") {
        ctx.addIssue({
          code: "custom",
          path: ["fixture_params"],
          message: "Only Navigate steps accept URL parameters.",
        });
      } else {
        const preview = workflowUrlPreview(step.url, step.fixture_params);
        if (preview.error) ctx.addIssue({ code: "custom", path: ["fixture_params"], message: preview.error });
      }
    }
    if (step.action === "checkpoint") {
      if (!step.name) {
        ctx.addIssue({ code: "custom", path: ["name"], message: "Name this checkpoint." });
      }
      if ([step.url, step.selector, step.value, step.secret_ref].some((value) => value != null)) {
        ctx.addIssue({ code: "custom", message: "A checkpoint records progress and cannot perform a page action." });
      }
    } else if (step.name != null) {
      ctx.addIssue({ code: "custom", path: ["name"], message: "Only checkpoints accept a name." });
    }
    if (step.action !== "navigate" && step.action !== "checkpoint" && !step.selector?.trim()) {
      ctx.addIssue({
        code: "custom",
        path: ["selector"],
        message: "Enter an element selector.",
      });
    }
    if (
      ["select", "assert_text"].includes(step.action) &&
      !step.value?.trim()
    ) {
      ctx.addIssue({
        code: "custom",
        path: ["value"],
        message: "Enter a value.",
      });
    }
    if (step.action === "fill" && (step.value == null) === !step.secret_ref) {
      ctx.addIssue({
        code: "custom",
        path: ["value"],
        message: "Enter one value or secret reference.",
      });
    }
  });

const legacyWorkflowSchema = z
  .object({
    name: z.string().trim().min(1, "Enter a workflow name.").max(100),
    schema_version: z.literal(1),
    steps: z.array(stepSchema).min(1, "Add a Navigate step.").max(40),
    initial_navigation_timeout_ms: initialNavigationTimeoutSchema,
  })
  .superRefine((workflow, ctx) => {
    if (workflowUrlParameterNames(workflow.steps).length > workflowParameterLimit) {
      ctx.addIssue({
        code: "custom",
        path: ["steps"],
        message: `Use at most ${workflowParameterLimit} distinct URL parameters per workflow.`,
      });
    }
    if (workflow.steps[0]?.action !== "navigate") {
      ctx.addIssue({
        code: "custom",
        path: ["steps", 0],
        message: "Start with a Navigate step.",
      });
    }
    const last = workflow.steps.at(-1)?.action;
    const assertions = ["assert_visible", "assert_text", "assert_not_visible"];
    if (workflow.steps.length === 1) {
      const [step] = workflow.steps;
      if (step.action === "navigate" && step.url?.includes("#")) {
        ctx.addIssue({
          code: "custom",
          path: ["steps", 0, "url"],
          message: "Use a URL without a fragment for a single Navigate step.",
        });
      }
    } else if (last && !assertions.includes(last)) {
      ctx.addIssue({
        code: "custom",
        path: ["steps"],
        message: "Finish with a success assertion.",
      });
    }
    const checkpointNames = new Set<string>();
    workflow.steps.forEach((step, index) => {
      if (step.action !== "checkpoint") return;
      if (index === 0 || !assertions.includes(workflow.steps[index - 1].action)) {
        ctx.addIssue({ code: "custom", path: ["steps", index], message: "Place each checkpoint immediately after an explicit assertion." });
      }
      if (step.name && checkpointNames.has(step.name)) {
        ctx.addIssue({ code: "custom", path: ["steps", index, "name"], message: "Checkpoint names must be unique." });
      }
      if (step.name) checkpointNames.add(step.name);
    });
  });

export const workflowScalarSchema = z.union([z.string(), z.number(), z.boolean()]);
export const workflowInputTypes = ["string", "number", "boolean"] as const;
const workflowName = z.string().regex(fixtureParameterName, "Start with a letter and use up to 32 letters, digits, or underscores.");
export const workflowInputDefinitionSchema = z.object({
  type: z.enum(workflowInputTypes),
  default: workflowScalarSchema.nullish(),
  required: z.boolean().optional(),
});
export const conditionKinds = ["visible", "hidden", "text_contains", "url_contains", "value_equals"] as const;
const workflowConditionSchema = z.object({
  kind: z.enum(conditionKinds),
  selector: z.string().nullish(),
  text: z.string().nullish(),
  value_ref: z.string().nullish(),
  value: workflowScalarSchema.nullish(),
});
const structuredStepFields = z.object({
  action: z.enum(actions),
  url: z.string().nullish(),
  fixture_params: z.record(z.string().regex(fixtureParameterName), fixtureParameterValue).nullish(),
  selector: z.string().nullish(),
  name: z.string().nullish(),
  value: z.string().nullish(),
  secret_ref: z.string().nullish(),
  timeout_ms: initialNavigationTimeoutSchema,
  post_navigation_wait_ms: z.number().int().min(0).max(postNavigationWaitMaxMs).nullish(),
  save_as: z.string().nullish(),
  value_ref: z.string().nullish(),
  condition: workflowConditionSchema.nullish(),
  max_iterations: z.number().int().min(1).max(100).nullish(),
  block_id: z.string().nullish(),
  block_version: z.number().int().positive().nullish(),
  tab: z.string().nullish(),
  frame: z.array(z.string()).max(12).nullish(),
});
export type Step = z.infer<typeof structuredStepFields> & { steps?: Step[] | null; else_steps?: Step[] | null };
const structuredStepSchema: z.ZodType<Step> = structuredStepFields.extend({
  get steps() { return z.array(structuredStepSchema).nullish(); },
  get else_steps() { return z.array(structuredStepSchema).nullish(); },
});
const structuredWorkflowSchema = z.object({
  name: z.string().trim().min(1, "Enter a workflow name.").max(100),
  schema_version: z.literal(2),
  steps: z.array(structuredStepSchema).min(1, "Add a step."),
  initial_navigation_timeout_ms: initialNavigationTimeoutSchema,
  inputs: z.record(workflowName, workflowInputDefinitionSchema).optional(),
  success_check: workflowConditionSchema.nullish(),
});
export const workflowSchema = z.preprocess((input) => (
  input != null && typeof input === "object" && !("schema_version" in input)
    ? { ...input, schema_version: 1 }
    : input
), z.discriminatedUnion("schema_version", [legacyWorkflowSchema, structuredWorkflowSchema]));

export type WorkflowScalar = z.infer<typeof workflowScalarSchema>;
export type WorkflowInputDefinition = z.infer<typeof workflowInputDefinitionSchema>;
export type WorkflowCondition = z.infer<typeof workflowConditionSchema>;
export type WorkflowInput = Omit<z.infer<typeof structuredWorkflowSchema>, "schema_version"> & { schema_version?: 1 | 2 };
export type Workflow = WorkflowInput & Pick<Schemas["WorkflowView"], "id" | "version">;
export type WorkflowBlock = { id: string; name: string; version: number; steps: Step[] };

export type TargetInput = Omit<
  Schemas["TargetInput"],
  "authorized" | "resource_origins"
> & {
  authorized: boolean;
  resource_origins: string[];
  concurrency?: number;
  acceptance_gate?: "verified_clean" | "observe_only";
};
export type Target = Schemas["TargetView"] & {
  concurrency?: number;
  acceptance_gate?: "verified_clean" | "observe_only";
};

export type RunInput = Omit<Schemas["RunInput"], "repetitions"> & {
  repetitions: number;
};
export type Run = Schemas["RunView"];
export type Trial = Schemas["TrialView"];
export type Report = Schemas["ReportView"];

export interface SystemStatus {
  browser: { available: boolean; reason?: string };
  worker: { ready: boolean };
  adscore: { configured: boolean; ready: boolean; reason?: string };
  recording?: { ready_workers: number; reporting_workers: number };
}

export type ValidationIssue = { path: (string | number)[]; message: string };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly issues: ValidationIssue[] = [],
  ) {
    super(message);
  }
}

function detailFrom(payload: unknown): unknown {
  if (typeof payload !== "object" || payload === null || !("detail" in payload))
    return;
  return payload.detail;
}

function validationIssues(detail: unknown): ValidationIssue[] {
  if (!Array.isArray(detail)) return [];
  return detail.flatMap((item: unknown) => {
    if (!item || typeof item !== "object" || !("msg" in item) || typeof item.msg !== "string") return [];
    const path = "loc" in item && Array.isArray(item.loc)
      ? item.loc.filter((part): part is string | number => (typeof part === "number" || typeof part === "string") && part !== "body")
      : [];
    return [{ path, message: item.msg.replace(/^Value error, /, "") }];
  });
}

export const PROJECT_KEY = "abrl.project";
export function currentProject() {
  return typeof window === "undefined"
    ? "default"
    : window.sessionStorage.getItem(PROJECT_KEY) || "default";
}

export async function request(
  path: string,
  token: string,
  options?: {
    method?: string;
    body?: unknown;
    signal?: AbortSignal;
    headers?: Record<string, string>;
  },
) {
  const response = await fetch(`${API_ROOT}/api/v1${path}`, {
    method: options?.method ?? "GET",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      "X-Project-ID": currentProject(),
      ...options?.headers,
      ...(options?.body === undefined
        ? {}
        : { "Content-Type": "application/json" }),
    },
    body:
      options?.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options?.signal,
    cache: "no-store",
    credentials: "include",
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const detail = detailFrom(payload);
    const issues = validationIssues(detail);
    const message = typeof detail === "string" ? detail : issues.map(({ path, message }) => (
      path.length ? `${path.join(".")}. ${message}` : message
    )).join(" ");
    throw new ApiError(
      message || `The API returned HTTP ${response.status}.`,
      response.status,
      issues,
    );
  }
  return response;
}

export async function api<T>(
  path: string,
  token: string,
  options?: Parameters<typeof request>[2],
): Promise<T> {
  const response = await request(path, token, options);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function arrayPage<T>(path: string, token: string): Promise<{items: T[]; total: number}> {
  const response = await request(path, token);
  const items = await response.json() as T[];
  const total = Number(response.headers.get("X-Total-Count") ?? items.length);
  return { items, total: Number.isFinite(total) ? total : items.length };
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

export async function streamRunEvents(
  id: string,
  token: string,
  signal: AbortSignal,
  onBatch: (events: RunEvent[]) => void,
) {
  const response = await request(
    `/runs/${encodeURIComponent(id)}/events`,
    token,
    { signal },
  );
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
        const payload = lines
          .find((line) => line.startsWith("data: "))
          ?.slice(6);
        if (!eventId || !payload) continue;
        const parsed: unknown = JSON.parse(payload);
        if (
          typeof parsed === "object" &&
          parsed &&
          "kind" in parsed &&
          "time" in parsed &&
          typeof parsed.kind === "string" &&
          typeof parsed.time === "string"
        ) {
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

export async function exportRun(
  id: string,
  format: "json" | "csv" | "html" | "pdf",
  token: string,
) {
  const response = await request(
    `/runs/${encodeURIComponent(id)}/export?format=${format}`,
    token,
  );
  saveFile(await response.blob(), `abrl-${id}.${format}`);
}

export const label = (value: string) => value.replaceAll("_", " ");
export const pct = (value: number | null) =>
  value === null
    ? "—"
    : new Intl.NumberFormat("en", {
        style: "percent",
        maximumFractionDigits: 1,
      }).format(value);
export const ms = (value: number | null) =>
  value === null ? "—" : `${Math.round(value).toLocaleString()} ms`;
export const dateTime = (value: string) =>
  new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
export const isActive = (status: string) =>
  ["queued", "pending", "running", "paused", "cancelling"].includes(
    status.toLowerCase(),
  );
