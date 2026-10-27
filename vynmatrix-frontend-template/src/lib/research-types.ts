import type { ControlData, FillsData, OverviewData, PnLData, StrategiesData } from "./types";

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export interface ResearchMetadata {
  instrument_id: number;
  source: string;
  provenance: string;
  currency: string;
  timeframe: string;
  timestamp_semantics: "bar_open" | "bar_close";
  adjustment_policy: "raw" | "adjusted";
  start?: string;
  end?: string;
}
export interface ResearchBar {
  ts: string; open: string; high: string; low: string; close: string; volume: string;
  bar_open?: string; bar_close?: string;
  instrument_id?: number; symbol?: string; source?: string; timeframe?: string; currency?: string | null;
}
export interface Dataset {
  dataset_id: string; import_id?: string; name: string; content_hash: string;
  original_hash?: string; metadata: ResearchMetadata; manifest: JsonObject;
  bar_count: number; created_at: string; eligibility: Json;
}
export interface ImportReceipt {
  import_id: string; content_hash: string | null; original_hash?: string; valid: boolean; created_at?: string;
  report: JsonObject; metadata: ResearchMetadata;
}
export interface ResearchDraft {
  draft_id: string; revision_id: string; revision: number; name: string;
  source: string; config: JsonObject; source_hash: string;
}
export type JobKind = "validate_strategy" | "train" | "predict" | "backtest";
export type JobStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
export interface ResearchJob {
  job_id: string; kind: JobKind; status: JobStatus; inputs: JsonObject;
  created_at: string; started_at: string | null; finished_at: string | null;
  error: Json; result: JsonObject | null; attempts: JsonObject[];
}
export interface ResearchModel {
  model_id: string; name: string; created_at: string; training_cutoff: string;
  metrics: JsonObject; artifact: JsonObject;
}
export type DraftSummary = Pick<ResearchDraft, "draft_id" | "revision_id" | "revision" | "name" | "source_hash">;
export type JobSummary = Pick<ResearchJob, "job_id" | "kind" | "status" | "created_at">;
export type ModelSummary = Pick<ResearchModel, "model_id" | "name" | "created_at" | "training_cutoff" | "metrics">;
export type ResearchDirectory = "datasets" | "drafts" | "jobs" | "models";
export interface ResearchWorkspace {
  as_of: string; datasets: Dataset[]; drafts: DraftSummary[]; jobs: JobSummary[];
  models: ModelSummary[];
  supervisor: { available: boolean; as_of: string | null; maintenance: boolean; generation: string | number | null };
  eligibility: Json; limits: Json;
  next_after?: Partial<Record<ResearchDirectory, string | null>>;
}
export interface MarketData {
  as_of: string;
  instruments: Array<{ instrument_id: number; symbol: string; currency: string | null; asset_class: string; [key: string]: Json | undefined }>;
  feeds: JsonObject[]; bars: ResearchBar[]; next_before: string | null;
}
export interface PaperData {
  as_of: string; safety: OverviewData["safety"];
  control: ControlData; strategies: StrategiesData | null; pnl: PnLData | null; fills: FillsData | null;
}
export interface PaperAccountInput {
  config_key: string; display_name: string; base_ccy: string;
  paper_initial_equity: string; paper_initial_cash: string;
}

export interface ResearchTemplate {
  template_id: "moving_average" | "return_model";
  name: string;
  source: string;
  requires_model: boolean;
  config: JsonObject;
}
export type SnapshotResult = { as_of: string; dataset: Dataset; import?: never } | { as_of: string; dataset: null; import: ImportReceipt };
