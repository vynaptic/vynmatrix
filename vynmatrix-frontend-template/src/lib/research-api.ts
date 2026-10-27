import { apiRequest } from "./api-client";
import type { Dataset, DraftSummary, JobSummary, ModelSummary, ImportReceipt, JsonObject, JobKind, MarketData, PaperAccountInput, PaperData, ResearchDraft, ResearchJob, ResearchMetadata, ResearchModel, ResearchWorkspace, ResearchTemplate, SnapshotResult } from "./research-types";

const base = "/ui/research";
const encode = encodeURIComponent;
function get<T>(path: string, signal?: AbortSignal) { return apiRequest<T>(`${base}${path}`, { signal }); }
function post<T>(path: string, data: unknown) { return apiRequest<T>(`${base}${path}`, { method: "POST", body: JSON.stringify(data) }); }
export const researchApi = {
  market: (params: URLSearchParams, signal?: AbortSignal) => get<MarketData>(`/market?${params}`, signal),
  datasets: (signal?: AbortSignal, after?: string) => get<{ as_of: string; datasets: Dataset[]; next_after: string | null }>(`/datasets${after ? `?after=${encode(after)}` : ""}`, signal),
  drafts: (after?: string) => get<{ as_of: string; drafts: DraftSummary[]; next_after: string | null }>(`/drafts${after ? `?after=${encode(after)}` : ""}`),
  jobs: (after?: string) => get<{ as_of: string; jobs: JobSummary[]; next_after: string | null }>(`/jobs${after ? `?after=${encode(after)}` : ""}`),
  models: (after?: string) => get<{ as_of: string; models: ModelSummary[]; next_after: string | null }>(`/models${after ? `?after=${encode(after)}` : ""}`),
  dataset: (id: string, signal?: AbortSignal, after?: number) => get<{ as_of: string; dataset: Dataset; bars: import("./research-types").ResearchBar[]; next_after: number | null }>(`/datasets/${encode(id)}?include_bars=true&limit=500${after === undefined ? "" : `&after=${after}`}`, signal),
  importCsv: (metadata: ResearchMetadata, csv: string) => post<{ as_of: string; import: ImportReceipt }>("/imports", { metadata, csv }),
  freeze: (import_id: string, expected_hash: string, name: string) => post<{ as_of: string; dataset: Dataset }>("/datasets", { import_id, expected_hash, name }),
  snapshot: (metadata: ResearchMetadata, name: string) => post<SnapshotResult>("/datasets/snapshot", { metadata, name }),
  exportDataset: (id: string) => get<JsonObject>(`/datasets/${encode(id)}/export`),
  templates: (signal?: AbortSignal) => get<{ as_of: string; templates: ResearchTemplate[] }>("/templates", signal),
  workspace: (signal?: AbortSignal) => get<ResearchWorkspace>("/workspace", signal),
  draft: (id: string, signal?: AbortSignal) => get<{ as_of: string; draft: ResearchDraft }>(`/drafts/${encode(id)}`, signal),
  saveDraft: (data: { request_id: string; name: string; source: string; config: JsonObject }) => post<{ as_of: string; draft: ResearchDraft }>("/drafts", data),
  saveRevision: (id: string, data: { expected_revision: number; name: string; source: string; config: JsonObject }) => post<{ as_of: string; draft: ResearchDraft }>(`/drafts/${encode(id)}/revisions`, data),
  exportDraft: (id: string, revision: string) => get<JsonObject>(`/drafts/${encode(id)}/export?revision_id=${encode(revision)}`),
  enqueue: (data: { request_id: string; kind: JobKind; inputs: JsonObject }) => post<{ as_of: string; job: ResearchJob }>("/jobs", data),
  job: (id: string, signal?: AbortSignal) => get<{ as_of: string; job: ResearchJob }>(`/jobs/${encode(id)}`, signal),
  cancelJob: (id: string) => post<{ as_of: string; job: ResearchJob }>(`/jobs/${encode(id)}/cancel`, {}),
  retryJob: (id: string, request_id: string) => post<{ as_of: string; job: ResearchJob }>(`/jobs/${encode(id)}/retry`, { request_id }),
  register: (data: { request_id: string; job_id: string; name: string }) => post<{ as_of: string; model: ResearchModel }>("/models", data),
  exportModel: (id: string) => get<JsonObject>(`/models/${encode(id)}/export`),
  paper: (signal?: AbortSignal) => get<PaperData>("/paper", signal),
  createPaperAccount: (data: PaperAccountInput) => post<unknown>("/paper/accounts", data),
  bindPaper: (data: { strategy_id: string; broker_account_id: number; mode: string }) => post<unknown>("/paper/bindings", data),
  updatePaperBinding: (id: number, data: { expected: JsonObject; changes: JsonObject }) => post<unknown>(`/paper/bindings/${id}`, data),
};
