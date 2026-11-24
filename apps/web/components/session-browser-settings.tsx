"use client";

import { useState } from "react";
import type { Adapter, BatchInput } from "@/lib/platform";
import { userAgentGroups } from "@/lib/user-agents";
import { Field, Select } from "./ui";

const devicePresets = [
  { id: "phone_small", name: "Compact phone", size: "360 × 800" },
  { id: "phone", name: "Phone", size: "390 × 844" },
  { id: "phone_large", name: "Large phone", size: "430 × 932" },
  { id: "tablet", name: "Tablet portrait", size: "768 × 1024" },
  { id: "tablet_landscape", name: "Tablet landscape", size: "1024 × 768" },
] as const;

export function devicePresetLabel(id: unknown): string | undefined {
  return devicePresets.find((preset) => preset.id === id)?.name;
}

export function deviceModeLabel(mode: unknown): string | undefined {
  switch (mode) {
    case "browser_default": return "Browser default";
    case "responsive_viewport": return "Responsive viewport";
    case "mobile_emulation": return "Mobile and touch emulation";
  }
}

export function allAdaptersSupport(
  ids: Adapter["id"][],
  adapters: Adapter[],
  capability: string,
): boolean {
  return ids.length > 0 && ids.every((id) =>
    adapters.find((adapter) => adapter.id === id)?.capabilities.includes(capability),
  );
}

export function validUserAgent(value: string | null | undefined): boolean {
  return value == null || (value.trim().length > 0 && value.length <= 1024 && !/[^ -~]/.test(value));
}

export function DevicePresetField({ value, onChange, selected, adapters }: {
  value: BatchInput["device_preset"];
  onChange: (value: BatchInput["device_preset"]) => void;
  selected: Adapter["id"][];
  adapters: Adapter[];
}) {
  const supported = allAdaptersSupport(selected, adapters, "responsive_viewport");
  const emulated = adapters.filter((adapter) =>
    selected.includes(adapter.id) && adapter.capabilities.includes("mobile_emulation"),
  );
  const hint = !supported
    ? "Select browsers that support responsive viewports to use a device preset."
    : emulated.length === 0
      ? "The selected browsers use the preset viewport dimensions."
      : emulated.length === selected.length
        ? "The selected browsers use mobile and touch emulation."
        : `${emulated.map((adapter) => adapter.name).join(", ")} uses mobile and touch emulation. The other browsers use the preset viewport dimensions.`;

  return <Field label="Device" hint={hint}>
    <Select value={value ?? ""} disabled={!supported && !value} onChange={(event) =>
      onChange((event.target.value || null) as BatchInput["device_preset"])
    }>
      <option value="">Browser default</option>
      {devicePresets.map((preset) =>
        <option key={preset.id} value={preset.id} disabled={!supported}>
          {preset.name} · {preset.size}
        </option>,
      )}
    </Select>
  </Field>;
}

const userAgentPresets = userAgentGroups.flatMap((group) => group.presets);
const customUserAgent = "custom";

export function userAgentLabel(value: string | null | undefined): string {
  return value == null ? "Browser default" : userAgentPresets.find((preset) => preset.value === value)?.label ?? "Custom";
}

export function UserAgentField({ value, onChange, supported }: {
  value: string | null | undefined;
  onChange: (value: string | null) => void;
  supported: boolean;
}) {
  const [search, setSearch] = useState("");
  const isCustom = userAgentLabel(value) === "Custom";
  const selection = isCustom ? customUserAgent : value ?? "";
  const terms = search.toLowerCase().split(/\s+/).filter(Boolean);
  const groups = userAgentGroups.map((group) => ({
    label: group.label,
    presets: group.presets.filter((preset) => {
      const description = `${group.label} ${preset.label}`.toLowerCase();
      return preset.value === value || terms.every((term) => description.includes(term));
    }),
  })).filter((group) => group.presets.length > 0);
  return <section className="stack compact-stack" aria-label="User-Agent settings">
    {supported && <Field label="Search User-Agent presets">
      <input type="search" placeholder="Browser or device" value={search} onChange={(event) => setSearch(event.target.value)} />
    </Field>}
    <Field label="User-Agent" hint={supported
      ? "Sets the User-Agent sent with requests and reported to page scripts."
      : "User-Agent overrides are available when every selected browser supports them."
    }>
      <Select value={selection} disabled={!supported && value == null} onChange={(event) =>
        onChange(event.target.value === customUserAgent ? "" : event.target.value || null)
      }>
        <option value="">Browser default</option>
        {groups.map((group) => <optgroup key={group.label} label={group.label}>
          {group.presets.map((preset) => <option key={preset.value} value={preset.value} disabled={!supported}>
            {preset.label}
          </option>)}
        </optgroup>)}
        <option value={customUserAgent} disabled={!supported}>Custom</option>
      </Select>
    </Field>
    {supported && groups.length === 0 && <p className="muted small-text">No matching presets.</p>}
    {isCustom && <Field label="Custom User-Agent" hint="Enter 1–1,024 printable ASCII characters.">
      <input
        required
        disabled={!supported}
        aria-invalid={!validUserAgent(value) || undefined}
        pattern="[ -~]+"
        maxLength={1024}
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>}
  </section>;
}
