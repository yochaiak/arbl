"use client";

import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { request, saveFile } from "@/lib/api";
import { Modal } from "./platform-ui";
import { ErrorNotice, Field } from "./ui";

function DiagnosticExportDialog({ token, sessionId, close }: {
  token: string;
  sessionId: string;
  close: () => void;
}) {
  const [baseline, setBaseline] = useState("");
  const [includeBaseline, setIncludeBaseline] = useState(true);
  const download = useMutation({
    mutationFn: async ({ baselineId, autoBaseline }: { baselineId: string; autoBaseline: boolean }) => {
      const query = new URLSearchParams();
      if (baselineId) query.set("baseline_id", baselineId);
      else if (autoBaseline) query.set("auto_baseline", "true");
      const search = query.toString();
      const response = await request(
        `/sessions/${encodeURIComponent(sessionId)}/diagnostic-bundle${search ? `?${search}` : ""}`,
        token,
      );
      if (response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
        throw new Error("The server did not return a diagnostic JSON bundle.");
      }
      return response.blob();
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (download.isPending) return;
    download.mutate({ baselineId: baseline.trim(), autoBaseline: includeBaseline }, {
      onSuccess: (bundle) => {
        saveFile(bundle, `abrl-diagnostic-bundle-${sessionId.slice(0, 12)}.json`);
        close();
      },
    });
  }

  return (
    <Modal title="Export diagnostic bundle" detail="Download sanitized session evidence as JSON." close={close}>
      <form className="stack compact-stack" onSubmit={submit}>
        <label className="checkbox-line">
          <input
            type="checkbox"
            checked={includeBaseline}
            onChange={(event) => setIncludeBaseline(event.target.checked)}
            disabled={download.isPending}
          />
          Include a clean baseline when available
        </label>
        <Field label="Baseline session ID (optional)" hint="Use a full ID or a unique prefix of at least 8 characters to choose a specific baseline from this project.">
          <input
            value={baseline}
            onChange={(event) => setBaseline(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            maxLength={36}
            disabled={download.isPending}
          />
        </Field>
        <ErrorNotice error={download.error} />
        <div className="actions">
          <button type="submit" className="button primary" disabled={download.isPending}>
            <Download size={16} /> {download.isPending ? "Preparing download…" : "Download JSON"}
          </button>
          <button type="button" className="button secondary" onClick={close}>Cancel</button>
        </div>
      </form>
    </Modal>
  );
}

export function DiagnosticBundleExport({ token, sessionId }: { token: string; sessionId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="button secondary small" onClick={() => setOpen(true)}>
        <Download size={14} /> Export diagnostic bundle
      </button>
      {open && <DiagnosticExportDialog token={token} sessionId={sessionId} close={() => setOpen(false)} />}
    </>
  );
}
