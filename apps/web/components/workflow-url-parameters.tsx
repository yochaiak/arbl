import {
  fixtureParameterMaxLength,
  validateWorkflowUrlOverrides,
  workflowUrlParameterNames,
  workflowUrlPreviews,
  type Workflow,
} from "@/lib/api";
import { ErrorNotice, Field } from "./ui";

export function WorkflowUrlPreview({ url, label }: { url: string; label: string }) {
  return (
    <div className="stack compact-stack">
      <span className="muted small-text">{label}</span>
      <code className="json-preview">{url}</code>
    </div>
  );
}

export function WorkflowUrlParameters({ steps, value, onChange, origin }: {
  steps: Workflow["steps"];
  value: Record<string, string>;
  onChange?: (value: Record<string, string>) => void;
  origin?: string;
}) {
  const names = workflowUrlParameterNames(steps);
  if (!names.length) return null;

  const { overrides, error } = validateWorkflowUrlOverrides(steps, value);
  const previews = workflowUrlPreviews(steps, overrides);
  return (
    <fieldset className="choice-fieldset stack compact-stack">
      <legend>Workflow URL parameters</legend>
      {onChange && (
        <>
          <p>Leave a field blank to use the saved defaults.</p>
          <div className="form-grid">
            {names.map((name) => {
              const defaults = previews.flatMap((preview) => {
                return Object.hasOwn(preview.defaults, name) ? [{ step: preview.step, value: preview.defaults[name] }] : [];
              });
              const firstDefault = defaults[0]?.value;
              const sameDefault = firstDefault && defaults.every((entry) => entry.value === firstDefault);
              const hint = sameDefault
                ? `Saved default ${firstDefault}`
                : defaults.map((entry) => `Step ${entry.step} default ${entry.value}`).join(" · ");
              return (
                <Field key={name} label={name} hint={hint}>
                  <input
                    maxLength={fixtureParameterMaxLength * 2}
                    value={Object.hasOwn(value, name) ? value[name] : ""}
                    onChange={(event) => onChange({ ...value, [name]: event.target.value })}
                  />
                </Field>
              );
            })}
          </div>
        </>
      )}
      {error ? <ErrorNotice error={error} /> : previews.map((preview) => preview.url && (
        <div className="stack compact-stack" key={preview.step}>
          <WorkflowUrlPreview
            label={`Step ${preview.step} URL preview`}
            url={origin && preview.url.startsWith("/") ? origin + preview.url : preview.url}
          />
          {!onChange && (
            <span className="muted small-text">
              Saved defaults · {Object.entries(preview.defaults)
                .map(([name, value]) => `${name} = ${value}`).join(" · ")}
            </span>
          )}
        </div>
      ))}
    </fieldset>
  );
}
