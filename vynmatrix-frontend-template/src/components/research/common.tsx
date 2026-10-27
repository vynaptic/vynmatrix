"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api-client";
import { researchApi } from "@/lib/research-api";
import type { Json, JsonObject, JobKind, ResearchDirectory, ResearchJob, ResearchWorkspace } from "@/lib/research-types";
import { Panel, StatusBadge } from "@/components/ui";
import { JobResults } from "./job-results";

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed. Retry when the API is reachable.";
}
export function RecordedTime({ value }: { value?: string | null }) { return value ? <time dateTime={value}>{value}</time> : <span>Not recorded</span>; }
export function JsonView({ value, label }: { value: Json | undefined; label?: string }) {
  return <pre className="research-json" aria-label={label}>{value === null || value === undefined ? "Unavailable" : JSON.stringify(value, null, 2)}</pre>;
}
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="form-field"><span>{label}</span>{children}</label>;
}
export function Feedback({ error, message }: { error?: string | null; message?: string | null }) {
  return <>{error && <div className="research-feedback error" role="alert">{error}</div>}{message && <p className="research-feedback" role="status">{message}</p>}</>;
}
export function downloadJson(name: string, value: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = name; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
export function requestId() { return crypto.randomUUID(); }
export function useResource<T>(loader: (signal: AbortSignal) => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => { setLoading(true); setRevision((value) => value + 1); }, []);
  useEffect(() => {
    const controller = new AbortController();
    loader(controller.signal).then((value) => { if (!controller.signal.aborted) { setData(value); setError(null); } })
      .catch((failure) => { if (!controller.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [loader, revision]);
  return { data, error, loading, refresh };
}
function mergeRows<T>(primary: T[], extra: T[], id: (row: T) => string): T[] {
  const rows = new Map(primary.map((row) => [id(row), row]));
  for (const row of extra) if (!rows.has(id(row))) rows.set(id(row), row);
  return [...rows.values()];
}
export function useWorkspace() {
  const resource = useResource(researchApi.workspace);
  const [pages, setPages] = useState<Partial<ResearchWorkspace>>({});
  const [paging, setPaging] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const pagingRef = useRef(false);
  const data = resource.data ? {
    ...resource.data,
    datasets: mergeRows(resource.data.datasets, pages.datasets ?? [], (row) => row.dataset_id),
    drafts: mergeRows(resource.data.drafts, pages.drafts ?? [], (row) => row.draft_id),
    jobs: mergeRows(resource.data.jobs, pages.jobs ?? [], (row) => row.job_id),
    models: mergeRows(resource.data.models, pages.models ?? [], (row) => row.model_id),
    next_after: { ...resource.data.next_after, ...pages.next_after },
  } : null;
  async function loadMore(directory: ResearchDirectory) {
    const after = data?.next_after[directory];
    if (!after || !data || pagingRef.current) return;
    pagingRef.current = true; setPaging(true); setPageError(null);
    try {
      const next = { ...data.next_after };
      if (directory === "datasets") { const result = await researchApi.datasets(undefined, after); next.datasets = result.next_after; setPages((old) => ({ ...old, datasets: mergeRows(data.datasets, result.datasets, (row) => row.dataset_id), next_after: next })); }
      if (directory === "drafts") { const result = await researchApi.drafts(after); next.drafts = result.next_after; setPages((old) => ({ ...old, drafts: mergeRows(data.drafts, result.drafts, (row) => row.draft_id), next_after: next })); }
      if (directory === "jobs") { const result = await researchApi.jobs(after); next.jobs = result.next_after; setPages((old) => ({ ...old, jobs: mergeRows(data.jobs, result.jobs, (row) => row.job_id), next_after: next })); }
      if (directory === "models") { const result = await researchApi.models(after); next.models = result.next_after; setPages((old) => ({ ...old, models: mergeRows(data.models, result.models, (row) => row.model_id), next_after: next })); }
    } catch (failure) { setPageError(errorMessage(failure)); }
    finally { pagingRef.current = false; setPaging(false); }
  }
  return { ...resource, data, error: resource.error || pageError, loadMore, paging };
}
export function WorkspaceMore({ workspace, directory }: { workspace: ReturnType<typeof useWorkspace>; directory: ResearchDirectory }) {
  return workspace.data?.next_after?.[directory] ? <button type="button" className="button secondary small" disabled={workspace.paging} onClick={() => workspace.loadMore(directory)}>Load more {directory}</button> : null;
}
export function Supervisor({ workspace }: { workspace: ResearchWorkspace }) {
  const supervisor = workspace.supervisor;
  return <div className="research-notice" role="status">
    <strong>{supervisor.maintenance ? "Research maintenance in progress" : supervisor.available ? "Research supervisor available" : "Research supervisor unavailable"}</strong>
    <span>Heartbeat: <RecordedTime value={supervisor.as_of} /> · Generation {supervisor.generation ?? "unknown"}</span>
    {(!supervisor.available || supervisor.maintenance) && <p>{supervisor.maintenance ? "New runs are disabled during maintenance." : "Jobs can be queued and will wait for a verified supervisor."} Start the supported <code>vmdev research worker</code> after maintenance and verify its heartbeat.</p>}
  </div>;
}
export function useResearchJob() {
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  const alive = useRef(true);
  const operating = useRef(false);
  const pending = useRef<{ key: string; id: string } | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const run = useCallback(async (operation: () => Promise<{ job: ResearchJob }>, quiet = false) => {
    const current = ++epoch.current; operating.current = true; if (!quiet) setBusy(true); setError(null);
    try { const result = await operation(); if (alive.current && current === epoch.current) setJob(result.job); }
    catch (failure) { if (alive.current && current === epoch.current) setError(errorMessage(failure)); }
    finally { if (alive.current && current === epoch.current) { operating.current = false; setBusy(false); } }
  }, []);
  const open = useCallback((id: string) => run(() => researchApi.job(id)), [run]);
  const jobId = job?.job_id;
  const jobStatus = job?.status;
  useEffect(() => {
    if (!jobId || !jobStatus || !["queued", "running"].includes(jobStatus)) return;
    const timer = setInterval(() => { if (!operating.current) void run(() => researchApi.job(jobId), true); }, 3000);
    return () => clearInterval(timer);
  }, [jobId, jobStatus, run]); // Input revisions are frozen by the server; polling only follows this job.
  const enqueue = (kind: JobKind, inputs: JsonObject) => {
    const key = JSON.stringify({ kind, inputs });
    if (pending.current?.key !== key) pending.current = { key, id: requestId() };
    const id = pending.current.id;
    return run(async () => { const result = await researchApi.enqueue({ request_id: id, kind, inputs }); pending.current = null; return result; });
  };
  const cancel = () => job && run(() => researchApi.cancelJob(job.job_id));
  const retry = () => {
    if (!job) return;
    const key = `retry:${job.job_id}:${job.attempts.length}`;
    if (pending.current?.key !== key) pending.current = { key, id: requestId() };
    const id = pending.current.id;
    return run(async () => { const result = await researchApi.retryJob(job.job_id, id); pending.current = null; return result; });
  };
  return { job, error, busy, open, enqueue, cancel, retry };
}
export function JobDetails({ controller }: { controller: ReturnType<typeof useResearchJob> }) {
  const { job, error, busy } = controller;
  if (!job) return <Feedback error={error} />;
  return <Panel title="Research job" subtitle="Inputs and attempts remain recorded across retries and restarts.">
    <Feedback error={error} />
    <div className="research-toolbar"><strong>{job.job_id}</strong><StatusBadge tone={job.status === "completed" ? "success" : job.status === "failed" ? "danger" : "neutral"}>{job.status}</StatusBadge><span>{job.kind}</span>
      <button className="button secondary small" disabled={busy} onClick={() => controller.open(job.job_id)}>Refresh job</button>
      {["queued", "running"].includes(job.status) && <button className="button secondary small" disabled={busy} onClick={() => controller.cancel()}>Cancel job</button>}
      {["failed", "cancelled", "interrupted"].includes(job.status) && <button className="button secondary small" disabled={busy} onClick={() => controller.retry()}>Retry job</button>}
    </div>
    <p>Created <RecordedTime value={job.created_at} /> · Started <RecordedTime value={job.started_at} /> · Finished <RecordedTime value={job.finished_at} /></p>
    {job.error && <div role="alert">{typeof job.error === "string" ? job.error : <JsonView value={job.error} />}</div>}
    <details><summary>Frozen job inputs</summary><JsonView value={job.inputs} /></details>
    <details open={job.status === "interrupted"}><summary>Attempts ({job.attempts.length})</summary><JsonView value={job.attempts} /></details>
    {job.result && <><h3>Recorded result</h3><JobResults kind={job.kind} result={job.result} /><button className="button secondary small" onClick={() => downloadJson(`research-${job.job_id}.json`, job)}>Export job result</button></>}
  </Panel>;
}
export function JobList({ workspace, controller, kinds, pagination }: { workspace: ResearchWorkspace; controller: ReturnType<typeof useResearchJob>; kinds: JobKind[]; pagination?: ReturnType<typeof useWorkspace> }) {
  const jobs = workspace.jobs.filter((job) => kinds.includes(job.kind));
  return <Panel title="Recorded jobs" subtitle={`Workspace observed ${workspace.as_of}`}>
    {jobs.length === 0 ? <p>{workspace.next_after?.jobs ? "No matching jobs in loaded history. Load more recorded jobs." : "No recorded jobs."}</p> : <div className="research-list">{jobs.map((job) => <button className="research-list-item" key={job.job_id} onClick={() => controller.open(job.job_id)} aria-label={`Open job ${job.job_id}`}><strong>{job.job_id}</strong><span>{job.kind} · {job.status}</span><RecordedTime value={job.created_at} /></button>)}</div>}
    {pagination && <WorkspaceMore workspace={pagination} directory="jobs" />}
  </Panel>;
}
export function isConflict(error: unknown) { return error instanceof ApiError && error.status === 409; }
