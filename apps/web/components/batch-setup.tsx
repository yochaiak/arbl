"use client";

import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Check, ChevronDown, ChevronLeft, ChevronRight, Search } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api, request, type Workflow, type WorkflowScalar } from "@/lib/api";
import { platformKey, type BatchInput, type PageResult } from "@/lib/platform";
import type { components } from "@/lib/schema";
import { ErrorNotice, Field, Select } from "./ui";

type CatalogItem = { id: string; name: string };
type Preflight = components["schemas"]["BatchPreflightView"];

export function BatchReadinessNotices({ notices }: { notices: Preflight["notices"] }) {
  if (!notices.length) return null;
  const visible = notices.slice(0, 10);
  const additional = notices.slice(10);
  function renderNotice(notice: Preflight["notices"][number]) {
    return <p className="notice" key={JSON.stringify(notice)}>
      {notice.group != null && <strong>Group {notice.group} · </strong>}{notice.message}
    </p>;
  }
  return <>
    {visible.map(renderNotice)}
    {additional.length > 0 && <details>
      <summary>{additional.length} additional notices</summary>
      {additional.map(renderNotice)}
    </details>}
  </>;
}

export function BatchCatalog<T extends CatalogItem>({
  token, path, title, value, initial, options, disabled, label, description, onSelect, optional, emptyLabel,
}: {
  token: string; path: string; title: string; value: string; initial: T[];
  options?: Record<string, string>; disabled?: (item: T) => boolean; label?: (item: T) => string;
  description?: (item: T) => string | undefined;
  onSelect: (item?: T) => void; optional?: boolean; emptyLabel?: string;
}) {
  const id = useId();
  const picker = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const searchField = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [highlight, setHighlight] = useState(0);
  const [selection, setSelection] = useState<T>();
  const limit = 30;
  const query = useQuery({
    queryKey: platformKey(token, "batch-catalog", path, options, search, offset),
    queryFn: async ({ signal }) => {
      const params = new URLSearchParams({ q: search, limit: String(limit), offset: String(offset), ...options });
      const response = await request(path + "?" + params.toString(), token, { signal });
      const data = await response.json() as PageResult<T> | T[];
      return Array.isArray(data)
        ? { items: data, total: Number(response.headers.get("x-total-count") || data.length) }
        : data;
    },
    enabled: open,
    staleTime: 5000,
  });
  useEffect(() => {
    if (!open) return;
    searchField.current?.focus();
    function handleOutsideClick(event: PointerEvent) {
      if (event.target instanceof Node && !picker.current?.contains(event.target)) setOpen(false);
    }
    document.addEventListener("pointerdown", handleOutsideClick);
    return () => document.removeEventListener("pointerdown", handleOutsideClick);
  }, [open]);

  const items = query.data?.items ?? (search ? [] : initial.slice(0, limit));
  const current = items.find((item) => item.id === value)
    ?? (selection?.id === value ? selection : initial.find((item) => item.id === value));
  const empty = emptyLabel ?? (optional ? "None" : `Select ${title.toLowerCase()}`);
  const rows: Array<T | undefined> = optional && !search && offset === 0 ? [undefined, ...items] : items;
  const selectable = rows.filter((item) => !item || !disabled?.(item));
  const active = selectable[Math.min(highlight, selectable.length - 1)];
  const hasActive = selectable.length > 0;
  const optionId = (item?: T) => `${id}-option-${item?.id ?? "empty"}`;

  function show() {
    setSearch("");
    setOffset(0);
    setHighlight(0);
    setOpen(true);
  }

  function select(item?: T) {
    setSelection(item);
    onSelect(item);
    setOpen(false);
    trigger.current?.focus();
  }

  function handleKeys(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      trigger.current?.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!hasActive) return;
      const next = Math.max(0, Math.min(selectable.length - 1, highlight + (event.key === "ArrowDown" ? 1 : -1)));
      setHighlight(next);
      document.getElementById(optionId(selectable[next]))?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (hasActive && (!query.isPending || !search)) select(active);
    }
  }

  function page(delta: number) {
    setOffset(offset + delta * limit);
    setHighlight(0);
    searchField.current?.focus();
  }

  return <div className="batch-picker" ref={picker} onBlur={(event) => {
    if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }}>
    <label id={`${id}-label`} htmlFor={`${id}-picker`}>{title}</label>
    <button ref={trigger} type="button" className="batch-picker-trigger" id={`${id}-picker`}
      aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? `${id}-options` : undefined}
      aria-labelledby={`${id}-label`} aria-describedby={`${id}-selection`}
      onClick={() => { if (open) setOpen(false); else show(); }}
      onKeyDown={(event) => { if (!open && event.key === "ArrowDown") { event.preventDefault(); show(); } }}>
      <span id={`${id}-selection`} className={!value ? "muted" : undefined}>
        {current ? label?.(current) ?? current.name : value || empty}
      </span>
      <ChevronDown size={16} aria-hidden="true" />
    </button>
    {open && <div className="batch-picker-menu">
      <div className="batch-picker-search">
        <Search size={15} aria-hidden="true" />
        <input ref={searchField} type="text" role="combobox" aria-label={`Find ${title.toLowerCase()}`}
          aria-expanded="true" aria-autocomplete="list" aria-controls={`${id}-options`}
          aria-activedescendant={hasActive ? optionId(active) : undefined}
          placeholder={`Find ${title.toLowerCase()}`} value={search} onKeyDown={handleKeys}
          onChange={(event) => { setSearch(event.target.value); setOffset(0); setHighlight(0); }} />
      </div>
      <div className="batch-picker-options" id={`${id}-options`} role="listbox" aria-labelledby={`${id}-label`}>
        {rows.map((item) => {
          const key = item?.id ?? "";
          const unavailable = !!item && !!disabled?.(item);
          const detail = item && description?.(item);
          return <button key={key} id={optionId(item)} type="button" role="option"
            className={hasActive && active?.id === item?.id ? "active" : undefined}
            aria-selected={key === value} disabled={unavailable} tabIndex={-1} onClick={() => select(item)}>
            <span>
              <strong>{item ? label?.(item) ?? item.name : empty}</strong>
              {detail && <small>{detail}</small>}
            </span>
            {key === value && <Check size={15} aria-hidden="true" />}
          </button>;
        })}
      </div>
      {!rows.length && !query.error && <p className="muted small-text" role="status">{query.isPending ? "Loading…" : "No matches"}</p>}
      <ErrorNotice error={query.error} />
      {!!query.data && query.data.total > limit && <div className="batch-picker-pages">
        <span>{offset + 1}–{Math.min(offset + limit, query.data.total)} of {query.data.total}</span>
        <button type="button" className="icon-button" aria-label={`Previous ${title.toLowerCase()} page`} disabled={!offset}
          onClick={() => page(-1)}><ChevronLeft size={16} /></button>
        <button type="button" className="icon-button" aria-label={`Next ${title.toLowerCase()} page`} disabled={offset + limit >= query.data.total}
          onClick={() => page(1)}><ChevronRight size={16} /></button>
      </div>}
    </div>}
  </div>;
}

export function useBatchPreflight(token: string, input: BatchInput, enabled: boolean) {
  const payload = JSON.stringify(input);
  const [settled, setSettled] = useState(payload);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(payload), 250);
    return () => clearTimeout(timer);
  }, [payload]);
  const query = useQuery({
    queryKey: platformKey(token, "batch-preflight", settled),
    queryFn: ({ signal }) => api<Preflight>("/batches/preflight", token, { method: "POST", body: JSON.parse(settled), signal }),
    enabled,
    retry: false,
    staleTime: 0,
  });
  return { ...query, current: enabled && settled === payload };
}

type BatchPresets = NonNullable<BatchInput["batch_presets"]>;
const presetChoices = {
  desktop_1280: { viewport: "1280 × 720", dpr: 1 },
  desktop_1440: { viewport: "1440 × 900", dpr: 2 },
  compact_390: { viewport: "390 × 844", dpr: 3 },
  tablet_768: { viewport: "768 × 1024", dpr: 2 },
} satisfies Record<BatchPresets["presets"][number], { viewport: string; dpr: number }>;
const localeChoices = {
  "en-US": "English (US) · New York",
  "en-GB": "English (UK) · London",
  "he-IL": "Hebrew (Israel) · Jerusalem",
} satisfies Record<NonNullable<BatchPresets["locales"]>[number], string>;

export function BatchPresetFields({ value, seed, available, contextAdapters, onChange }: {
  value: BatchInput["batch_presets"];
  seed: BatchInput["seed"];
  available: boolean;
  contextAdapters: string[];
  onChange: (patch: Pick<BatchInput, "batch_presets" | "seed">) => void;
}) {
  const mode = value ? value.mode ?? "fixed" : "";
  const locales = value?.locales ?? ["en-US"];
  const hasContextPresets = contextAdapters.length > 0;
  const contextNames = contextAdapters.join(", ");
  function setMode(next: BatchPresets["mode"] | "") {
    if (!next) {
      onChange({ batch_presets: null, seed: null });
      return;
    }
    const presets = value?.presets.length ? value.presets : ["desktop_1280"] as const;
    const selectedLocales: BatchPresets["locales"] = hasContextPresets && !locales.length ? ["en-US"] : value?.locales;
    onChange({ batch_presets: {
      catalog_version: 1,
      mode: next,
      presets: next === "fixed" ? presets.slice(0, 1) : [...presets],
      locales: next === "fixed" ? selectedLocales?.slice(0, 1) : selectedLocales,
    } });
  }
  return <fieldset className="choice-fieldset">
    <legend>Batch presets</legend>
    <div className="form-grid">
      <Field label="Preset distribution" hint={hasContextPresets
        ? `The batch assigns a viewport to each session. DPR, locale, and timezone also apply to ${contextNames}.`
        : "The batch assigns a viewport to each session."}>
        <Select value={mode} disabled={!available && !value}
          onChange={(event) => setMode(event.target.value as BatchPresets["mode"] | "")}>
          <option value="">Off</option>
          <option value="fixed">Fixed</option>
          <option value="round_robin">Round robin</option>
          <option value="seeded">Seeded sampling</option>
        </Select>
      </Field>
      {value && <Field label="Batch seed" hint="Leave blank to generate and save a seed.">
        <input type="number" min={0} max={2147483647} step={1} value={seed ?? ""}
          onChange={(event) => onChange({ seed: event.target.value === "" ? null : event.target.valueAsNumber })} />
      </Field>}
      {value && <>
        <Field label="Viewport presets" hint={mode === "fixed" ? undefined : "Select one or more sizes."}>
          <Select multiple={mode !== "fixed"} required value={mode === "fixed" ? value.presets[0] : value.presets}
            onChange={(event) => onChange({ batch_presets: {
              ...value,
              presets: Array.from(event.target.selectedOptions, (option) => option.value as BatchPresets["presets"][number]),
            } })}>
            {Object.entries(presetChoices).map(([id, { viewport, dpr }]) =>
              <option key={id} value={id}>{viewport}{hasContextPresets ? ` · Context DPR ${dpr}` : ""}</option>,
            )}
          </Select>
        </Field>
        {hasContextPresets && <Field label="Locale and timezone" hint={mode === "fixed" ? undefined : "Select one or more locales."}>
          <Select multiple={mode !== "fixed"} required value={mode === "fixed" ? locales[0] : locales}
            onChange={(event) => onChange({ batch_presets: {
              ...value,
              locales: Array.from(event.target.selectedOptions, (option) => option.value as keyof typeof localeChoices),
            } })}>
            {Object.entries(localeChoices).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </Select>
        </Field>}
      </>}
    </div>
    {!available && <p className="muted small-text">Batch presets require supported browsers and browser-default settings.</p>}
  </fieldset>;
}

export function WorkflowValuesFields({ workflow, value, onChange }: {
  workflow?: Workflow; value: Record<string, WorkflowScalar>;
  onChange: (value: Record<string, WorkflowScalar>) => void;
}) {
  const entries = Object.entries(workflow?.inputs ?? {});
  if (!entries.length) return null;
  function change(name: string, valueForInput: WorkflowScalar | undefined) {
    const next = { ...value };
    if (valueForInput === undefined) delete next[name];
    else next[name] = valueForInput;
    onChange(next);
  }
  return <fieldset className="choice-fieldset">
    <legend>Workflow inputs</legend>
    <div className="form-grid">{entries.map(([name, definition]) => {
      const current = value[name];
      const mandatory = definition.required && definition.default == null;
      return <Field key={name} label={name} hint={definition.default == null ? undefined : `Default: ${definition.default}`}>
        {definition.type === "boolean" ? (
          <Select required={mandatory} value={current == null ? "" : String(current)} onChange={(event) => (
            change(name, event.target.value ? event.target.value === "true" : undefined)
          )}>
            <option value="">Use default</option><option value="true">True</option><option value="false">False</option>
          </Select>
        ) : <input
          type={definition.type === "number" ? "number" : "text"} step={definition.type === "number" ? "any" : undefined}
          required={mandatory} value={current == null ? "" : String(current)}
          onChange={(event) => {
            const text = event.target.value;
            change(name, !text && definition.type === "number" ? undefined : definition.type === "number" ? Number(text) : text);
          }}
        />}
      </Field>;
    })}</div>
  </fieldset>;
}
