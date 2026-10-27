import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MarketDataPage from "@/app/(workspace)/market-data/page";
import MlLabPage from "@/app/(workspace)/ml-lab/page";
import StrategyNewPage from "@/app/(workspace)/strategies/new/page";
import { clearAdminKey, isAuthenticated, setAdminKey } from "@/lib/api-client";
import { researchApi } from "@/lib/research-api";

const at = "2026-10-27T12:34:56.123456Z";
// These spellings are part of the server's content identity, including values
// JavaScript Number cannot represent or stringify without changing them.
const numbers = '{"regularization":1.0,"negative_zero":-0.0,"large_integer":9007199254740993,"text":"café"}';
const wires = {
  model: `{\r\n"as_of":"${at}","artifact":${numbers}}\r\n`,
  dataset: `{"as_of":"${at}","original_csv":"ts,open,high,low,close,volume\\r\\n","manifest":${numbers}}\n`,
  draft: `{"as_of":"${at}","source":"class UserStrategy: pass","config":{"parameters":${numbers}}}\r\n`,
  job: `{\r\n"as_of":"${at}","job":{"job_id":"job-1","kind":"train","status":"completed","inputs":{},"created_at":"${at}","started_at":"${at}","finished_at":"${at}","error":null,"attempts":[],"result":{"model":${numbers}}}}\n`,
};
const metadata = { instrument_id: 1, source: "archive", provenance: "Owner archive", currency: "EUR", timeframe: "1d", timestamp_semantics: "bar_open", adjustment_policy: "raw" };
const dataset = { dataset_id: "data-1", name: "Archive", content_hash: "content-hash", metadata, manifest: {}, bar_count: 1200, created_at: at, eligibility: { training: true, backtest: true } };
const draft = { draft_id: "draft-1", revision_id: "revision-1", revision: 1, name: "Owner strategy", source: "class UserStrategy: pass", config: { class_name: "UserStrategy", strategy_id: "owner_strategy", parameters: {} }, source_hash: "source-hash" };
const model = { model_id: "model-1", name: "Owner model", created_at: at, training_cutoff: at, metrics: {} };
const workspace = { as_of: at, datasets: [dataset], drafts: [draft], models: [model], jobs: [{ job_id: "job-1", kind: "train", status: "completed", created_at: at }], supervisor: { available: true, as_of: at, maintenance: false, generation: "generation-1" }, eligibility: {}, limits: {} };
const response = (body: string, status = 200) => new Response(body, { status, headers: { "Content-Type": "application/json" } });
function server(jobExportFails = false) {
  let jobReads = 0;
  const requests = vi.fn(async (url: string) => {
    const path = url.replace("/api/ui/research", "");
    if (path === "/models/model-1/export") return response(wires.model);
    if (path === "/datasets/data-1/export") return response(wires.dataset);
    if (path === "/drafts/draft-1/export?revision_id=revision-1") return response(wires.draft);
    if (path === "/jobs/job-1") { jobReads++; return jobExportFails && jobReads > 1 ? response('{"detail":"Job export unavailable"}', 503) : response(wires.job); }
    if (path === "/workspace") return response(JSON.stringify(workspace));
    if (path === "/templates") return response(JSON.stringify({ as_of: at, templates: [] }));
    if (path === "/drafts/draft-1") return response(JSON.stringify({ as_of: at, draft }));
    if (path === "/datasets") return response(JSON.stringify({ as_of: at, datasets: [dataset], next_after: null }));
    if (path.startsWith("/datasets/data-1?")) return response(JSON.stringify({ as_of: at, dataset, bars: [], next_after: null }));
    if (path.startsWith("/market?")) return response(JSON.stringify({ as_of: at, instruments: [], feeds: [], bars: [], next_before: null }));
    throw new Error(`Unexpected request: ${path}`);
  });
  vi.stubGlobal("fetch", requests);
  return { requests, jobReads: () => jobReads };
}
function captureDownloads() {
  const blobs: Blob[] = []; const names: string[] = [];
  const OriginalURL = URL;
  vi.stubGlobal("URL", class extends OriginalURL {
    static createObjectURL(blob: Blob) { blobs.push(blob); return "blob:research-export"; }
    static revokeObjectURL() {}
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { names.push(this.download); });
  return { blobs, names };
}
async function bytes(blob: Blob): Promise<Uint8Array> {
  if (typeof blob.arrayBuffer === "function") return new Uint8Array(await blob.arrayBuffer());
  return new Promise((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer)); reader.onerror = () => reject(reader.error); reader.readAsArrayBuffer(blob);
  });
}
beforeEach(() => { clearAdminKey(); setAdminKey("export-owner-key"); });

async function exportFromPage(kind: keyof typeof wires) {
  if (kind === "model") { render(<MlLabPage />); fireEvent.click(await screen.findByRole("button", { name: "Download Owner model" })); }
  if (kind === "dataset") { render(<MarketDataPage />); fireEvent.change(await screen.findByLabelText("Browse dataset"), { target: { value: "data-1" } }); fireEvent.click(screen.getByRole("button", { name: "Export dataset" })); }
  if (kind === "draft") { render(<StrategyNewPage />); fireEvent.change(await screen.findByLabelText("Open draft"), { target: { value: "draft-1" } }); await screen.findByDisplayValue("class UserStrategy: pass"); fireEvent.click(screen.getByRole("button", { name: "Export saved revision" })); }
  if (kind === "job") { render(<MlLabPage />); fireEvent.click(await screen.findByRole("button", { name: "Open job job-1" })); fireEvent.click(await screen.findByRole("button", { name: "Export job result" })); }
}
describe("research export content identity", () => {
  it.each(["model", "dataset", "draft", "job"] as const)("downloads %s JSON bytes without parsing/reformatting the wire response", async (kind) => {
    const boundary = server(); const downloads = captureDownloads();
    await exportFromPage(kind);
    await waitFor(() => expect(downloads.blobs).toHaveLength(1));
    expect(Array.from(await bytes(downloads.blobs[0]))).toEqual(Array.from(new TextEncoder().encode(wires[kind])));
    expect(downloads.blobs[0].type).toBe("application/json");
    expect(downloads.names).toEqual([{ model: "model-model-1.json", dataset: "dataset-data-1.json", draft: "strategy-draft-1-revision-1.json", job: "research-job-1.json" }[kind]]);
    for (const [, options] of boundary.requests.mock.calls as unknown as [string, RequestInit][]) expect(new Headers(options.headers).get("X-Admin-Key")).toBe("export-owner-key");
    if (kind === "job") expect(boundary.jobReads()).toBe(2);
  });

  it("does not export a parsed cached job when the authenticated export read fails", async () => {
    server(true); const downloads = captureDownloads();
    await exportFromPage("job");
    await screen.findByText("Job export unavailable");
    expect(downloads.blobs).toHaveLength(0);
  });
});

describe("research export authentication and errors", () => {
  it("clears the current rejected credential and refuses a download", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response('{"detail":"Export key rejected"}', 401)));
    await expect(researchApi.exportModel("model-1")).rejects.toMatchObject({ status: 401, message: "Export key rejected" });
    expect(isAuthenticated()).toBe(false);
  });

  it("does not clear a replacement credential when an older export is rejected", async () => {
    let complete!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { complete = resolve; })));
    const pending = researchApi.exportModel("model-1");
    setAdminKey("replacement-owner-key"); complete(response('{"detail":"Old export key rejected"}', 401));
    await expect(pending).rejects.toMatchObject({ status: 401, message: "Old export key rejected" });
    expect(isAuthenticated()).toBe(true); expect(sessionStorage.getItem("adminKey")).toBe("replacement-owner-key");
  });

  it.each([
    ['{"detail":"Export denied"}', 403, "Export denied"],
    ["Export service unavailable", 503, "Export service unavailable"],
    ["<html>Proxy failure</html>", 502, "Request failed (502)"],
  ])("keeps a failed export on the shared error path", async (body, status, message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status })));
    await expect(researchApi.exportModel("model-1")).rejects.toMatchObject({ status, message });
  });
});
