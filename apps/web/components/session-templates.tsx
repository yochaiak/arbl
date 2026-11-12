"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { components } from "@/lib/schema";
import { type BatchInput, type PageResult, platformKey } from "@/lib/platform";
import { Badge, ErrorNotice, Field, Select } from "./ui";
import { Pagination } from "./platform-ui";

type Template = components["schemas"]["SessionTemplateView"];

export function SessionTemplates({ token, value, onChange }: {
  token: string; value: BatchInput; onChange: (value: BatchInput) => void;
}) {
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(value.session_template_id ?? "");
  const [version, setVersion] = useState(value.session_template_version ?? 1);
  const [name, setName] = useState("");
  const [loaded, setLoaded] = useState<Template>();
  const query = useQueryClient();
  const templates = useQuery({
    queryKey: platformKey(token, "session-templates", search, offset),
    queryFn: () => api<PageResult<Template>>(`/session-templates?limit=20&offset=${offset}&q=${encodeURIComponent(search)}&order_by=name&direction=asc`, token),
  });
  function adopt(template: Template) {
    setLoaded(template); setSelected(template.id); setVersion(template.version); setName(template.name);
    onChange({ ...template.batch, idempotency_key: crypto.randomUUID() } as BatchInput);
  }
  const load = useMutation({
    mutationFn: () => api<Template>(`/session-templates/${selected}/versions/${version}`, token),
    onSuccess: adopt,
  });
  const save = useMutation({
    mutationFn: (revision: boolean) => api<Template>(revision ? `/session-templates/${selected}` : "/session-templates", token, {
      method: revision ? "PUT" : "POST",
      body: { name: name.trim(), batch: { ...value, session_template_id: null, session_template_version: null } },
    }),
    onSuccess: (template) => { adopt(template); void query.invalidateQueries({ queryKey: platformKey(token, "session-templates") }); },
  });
  return <section className="choice-fieldset stack compact-stack" aria-label="Saved session templates">
    <div className="subsection-header"><strong>Reusable session configuration</strong><Badge value={value.session_template_id ? `Pinned v${value.session_template_version}` : "Editable draft"} /></div>
    <p className="muted small-text">Templates preserve browser, preparation, proxy, and workflow settings with immutable target/workflow versions. Loading never launches a session.</p>
    <div className="form-grid">
      <Field label="Search session templates"><input maxLength={100} value={search} onChange={(event) => { setSearch(event.target.value); setOffset(0); }} placeholder="Find a saved configuration" /></Field>
      <Field label="Saved session template"><Select value={selected} onChange={(event) => {
        setSelected(event.target.value);
        const item = templates.data?.items.find((item) => item.id === event.target.value);
        if (item) { setVersion(item.version); setName(item.name); }
      }}>
        <option value="">Choose a template</option>
        {selected && !templates.data?.items.some((item) => item.id === selected) && <option value={selected}>{loaded?.name ?? selected}</option>}
        {templates.data?.items.map((item) => <option value={item.id} key={item.id}>{item.name} · v{item.version}</option>)}
      </Select></Field>
      <Field label="Template version" hint="Enter any saved immutable version."><input type="number" min={1} value={version} onChange={(event) => setVersion(Number(event.target.value))} /></Field>
      <Field label="Template name" hint="Used when saving a new template or revision."><input maxLength={100} value={name} onChange={(event) => setName(event.target.value)} /></Field>
    </div>
    {templates.data && templates.data.total > 20 && <Pagination total={templates.data.total} offset={offset} limit={20} onChange={setOffset} />}
    <div className="actions">
      <button type="button" className="button secondary small" disabled={!selected || version < 1 || load.isPending} onClick={() => load.mutate()}>Load template version</button>
      <button type="button" className="button secondary small" disabled={!name.trim() || save.isPending || !value.target_id || !value.workflow_id} onClick={() => save.mutate(false)}>Save new template</button>
      <button type="button" className="button secondary small" disabled={!selected || !name.trim() || save.isPending} onClick={() => save.mutate(true)}>Save template revision</button>
      {value.session_template_id && <button type="button" className="button secondary small" onClick={() => onChange({ ...value, session_template_id: null, session_template_version: null })}>Detach to edit</button>}
    </div>
    {value.session_template_id && <p className="notice">Configuration is pinned and locked. Detach explicitly before editing. Saving a new revision pins the current target, workflow, and proxy versions.</p>}
    {loaded && value.session_template_id === loaded.id && <p className="muted small-text">Target v{loaded.target_version} · workflow v{loaded.workflow_version} · proxy {loaded.proxy_policy_version == null ? "none" : `v${loaded.proxy_policy_version}`}</p>}
    <ErrorNotice error={templates.error || load.error || save.error} />
  </section>;
}
