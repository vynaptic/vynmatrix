import type { BindingReadiness } from "@/lib/types";
import { StatusBadge } from "@/components/ui";
import { RecordedTime } from "./common";

const checkStates = {
  ok: { label: "OK", tone: "success" },
  warn: { label: "Warning", tone: "warning" },
  blocked: { label: "Blocked", tone: "danger" },
  unknown: { label: "Unknown", tone: "neutral" },
} as const;

export function RecordedBindingReadiness({ bindingId, readiness, asOf }: { bindingId: number; readiness?: BindingReadiness | null; asOf?: string }) {
  return <section className="binding-readiness" aria-label={`Binding ${bindingId} readiness`}>
    <div className="research-toolbar"><h4>Recorded readiness</h4><StatusBadge tone={readiness ? readiness.ready ? "success" : "danger" : "neutral"}>{readiness ? readiness.ready ? "Ready" : "Blocked" : "Readiness unavailable"}</StatusBadge></div>
    {!readiness ? <p>The server did not return a readiness checklist for this binding. Refresh paper state to retry.</p> : <>
      <p>Checklist observed <RecordedTime value={asOf} />. Pending mode or risk edits do not update this recorded snapshot.</p>
      <ul className="binding-readiness-checks">{readiness.checks.map((check) => {
        const state = checkStates[check.state];
        return <li key={check.key}><div><strong>{check.label}</strong><StatusBadge tone={state.tone}>{state.label}</StatusBadge></div><p>{check.detail}</p><span>{check.at ? <RecordedTime value={check.at} /> : "No observation time recorded"}</span></li>;
      })}</ul>
    </>}
  </section>;
}
