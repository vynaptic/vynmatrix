import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MarketDataPage from "@/app/(workspace)/market-data/page";
import MlLabPage from "@/app/(workspace)/ml-lab/page";
import StrategyNewPage from "@/app/(workspace)/strategies/new/page";
import PaperTradingPage from "@/app/(workspace)/paper-trading/page";
import { setAdminKey } from "@/lib/api-client";

const at = "2026-10-27T12:34:56.123456Z";
const metadata = { instrument_id: 1, source: "recorded-feed", currency: "EUR", timeframe: "1d", timestamp_semantics: "bar_open", adjustment_policy: "raw", provenance: "Owner archive" };
const dataset = { dataset_id: "data-1", name: "Recorded archive", content_hash: "content-hash", created_at: at, metadata, manifest: { provenance_strength: "owner_supplied", row_count: 1200 }, eligibility: { training: true, backtest: true, training_refusals: [], backtest_refusals: [] }, bar_count: 1200 };
const draft = { draft_id: "draft-1", revision_id: "revision-1", revision: 1, name: "Archive strategy", source: "class ArchiveStrategy: pass", config: {}, source_hash: "source-hash" };
const model = { model_id: "model-1", name: "Archive model", created_at: at, training_cutoff: at, metrics: { rmse: "0.1" }, artifact: {} };
const job = { job_id: "job-1", kind: "train", status: "queued", inputs: { dataset_id: "data-1", horizon: 1 }, created_at: at, started_at: null, finished_at: null, error: null, result: null, attempts: [] };
const workspace = { as_of: at, datasets: [dataset], drafts: [draft], jobs: [], models: [model], supervisor: { available: true, as_of: at, maintenance: false, generation: "generation-1" }, eligibility: {}, limits: {} };
const market = { as_of: at, instruments: [{ instrument_id: 1, symbol: "ARCHIVE", currency: "EUR", asset_class: "equity" }], feeds: [{ instrument_id: 1, source: "recorded-feed", timeframe: "1d", last_at: at }], bars: [], next_before: null };
const binding = { binding_id: 1, strategy_id: "released-1", account_id: 1, mode: "off", asset_score_threshold: "0.5", max_position_pct: "0.1", max_total_exposure_pct: "0.5", max_daily_loss_pct: "0.02", max_open_positions: 3, instruments_allowed: null, asset_classes_allowed: ["equity"] };
const account = { account_id: 1, config_key: "local-paper", display_name: "Local paper", broker_code: "paper", broker_name: "Paper", environment: "paper", base_ccy: "EUR", status: "connected", external_ref: null, paper_initial_equity: "100.123456789", paper_initial_cash: "100.123456789", credential: { present: false, status: null, expires_at: null } };
const paper = { as_of: at, safety: { execution_mode: "paper", allow_live: false, environment: "paper" }, control: { as_of: at, owner: null, accounts: [account], bindings: [binding], strategies: [{ strategy_id: "released-1", name: "Released strategy", asset_class: "equity", released: true }], activity: [], modes: ["off", "close_only", "trading"], limits: {} }, strategies: { as_of: at, strategies: [] }, pnl: { as_of: at, days: 90, accounts: [] }, fills: { as_of: at, fills: [], next_before: null } };
type Handler = (path: string, body: Record<string, unknown> | null) => unknown | Promise<unknown>;
function server(handler?: Handler) {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    const path = url.replace("/api/ui/research", "");
    const body = options?.body ? JSON.parse(String(options.body)) : null;
    const custom = await handler?.(path, body);
    if (custom instanceof Response) return custom;
    const value = custom ?? (path.startsWith("/market") ? market : path === "/workspace" ? workspace : path === "/templates" ? { as_of: at, templates: [] } : path === "/datasets" ? { as_of: at, datasets: [dataset] } : path === "/paper" ? paper : {});
    return new Response(JSON.stringify(value), { status: 200 });
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
beforeEach(() => setAdminKey("test-owner-key"));

describe("recorded market datasets", () => {
  it("validates a CSV, freezes only its receipt hash and sends authenticated requests", async () => {
    const requests = server((path, body) => {
      if (path === "/imports") {
        expect(body?.csv).toContain("ts,open,high,low,close,volume");
        return { as_of: at, import: { import_id: "import-1", content_hash: "validated-hash", valid: true, report: { row_count: 1, errors: [] }, metadata } };
      }
      if (path === "/datasets" && body) {
        expect(body).toEqual({ import_id: "import-1", expected_hash: "validated-hash", name: "Owner archive" });
        return { as_of: at, dataset };
      }
    });
    render(<MarketDataPage />);
    await screen.findByLabelText("CSV file");
    change("Import instrument", "1"); change("Import source", "recorded-feed"); change("Import currency", "EUR"); change("Import timeframe", "1d"); change("Import provenance", "Owner archive"); change("Dataset name", "Owner archive");
    const file = new File(["ts,open,high,low,close,volume\n2026-01-01T00:00:00Z,10,12,9,11,2"], "archive.csv", { type: "text/csv" });
    fireEvent.change(screen.getByLabelText("CSV file"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "Validate CSV" }));
    await screen.findByText("validated-hash");
    fireEvent.click(screen.getByRole("button", { name: "Freeze dataset" }));
    await screen.findByText(/Dataset frozen/);
    expect(requests.mock.calls.every(([, options]) => new Headers(options?.headers).get("X-Admin-Key") === "test-owner-key")).toBe(true);
  });

  it("retains invalid receipt diagnostics and prevents freeze", async () => {
    server((path) => path === "/imports" ? { as_of: at, import: { import_id: "bad-1", content_hash: "bad-hash", valid: false, metadata, report: { errors: ["Row 2 has nonfinite close"], row_count: 0 } } } : undefined);
    render(<MarketDataPage />); await screen.findByLabelText("CSV file");
    change("Import instrument", "1"); change("Import source", "recorded-feed"); change("Import currency", "EUR"); change("Import timeframe", "1d"); change("Import provenance", "Owner archive");
    fireEvent.change(screen.getByLabelText("CSV file"), { target: { files: [new File(["bad"], "bad.csv")] } });
    fireEvent.click(screen.getByRole("button", { name: "Validate CSV" }));
    await screen.findByText(/nonfinite close/);
    expect((screen.getByRole("button", { name: "Freeze dataset" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("suppresses an earlier dataset detail response after another selection", async () => {
    let resolveFirst!: (data: unknown) => void;
    server((path) => {
      if (path === "/datasets") return { as_of: at, datasets: [dataset, { ...dataset, dataset_id: "data-2", name: "Later archive" }] };
      if (path.startsWith("/datasets/data-1?")) return new Promise((resolve) => { resolveFirst = resolve; });
      if (path.startsWith("/datasets/data-2?")) return { as_of: at, dataset: { ...dataset, dataset_id: "data-2", name: "Later archive" }, bars: [{ ts: at, open: "8.123456789", high: "9", low: "8", close: "8.5", volume: "1" }] };
    });
    render(<MarketDataPage />); await screen.findByLabelText("Browse dataset");
    change("Browse dataset", "data-1"); await waitFor(() => expect(resolveFirst).toBeTypeOf("function"));
    change("Browse dataset", "data-2"); await screen.findByText("8.123456789");
    resolveFirst({ as_of: at, dataset, bars: [{ ts: at, open: "999", high: "999", low: "999", close: "999", volume: "1" }] });
    await waitFor(() => expect(screen.queryByText("999")).toBeNull());
    expect(screen.getByText("8.123456789")).toBeTruthy();
  });
});

describe("durable model research", () => {
  it("queues immutable training inputs, cancels and retains a cancelled attempt", async () => {
    server((path, body) => {
      if (path === "/jobs") {
        expect(body?.kind).toBe("train"); expect(body?.inputs).toEqual({ dataset_id: "data-1", horizon: 2 }); expect(body?.request_id).toBeTypeOf("string");
        return { as_of: at, job };
      }
      if (path === "/jobs/job-1/cancel") return { as_of: at, job: { ...job, status: "cancelled", attempts: [{ status: "cancelled", finished_at: at }] } };
      if (path === "/jobs/job-1") return { as_of: at, job };
    });
    render(<MlLabPage />); await screen.findByLabelText("Training dataset");
    change("Training dataset", "data-1"); change("Forecast horizon", "2");
    fireEvent.click(screen.getByRole("button", { name: "Train model" }));
    await screen.findByText("job-1");
    fireEvent.click(screen.getByRole("button", { name: "Cancel job" }));
    await screen.findByText("cancelled", { selector: ".status-badge" });
    expect(screen.getByText(/Attempts/)).toBeTruthy();
  });

  it("shows interrupted jobs, retries as another attempt and registers a completed training run", async () => {
    const interrupted = { ...job, status: "interrupted", error: "Lease expired", attempts: [{ status: "interrupted" }] };
    server((path) => {
      if (path === "/workspace") return { ...workspace, jobs: [interrupted] };
      if (path === "/jobs/job-1") return { as_of: at, job: interrupted };
      if (path === "/jobs/job-1/retry") return { as_of: at, job: { ...job, status: "completed", attempts: [{ status: "interrupted" }, { status: "completed" }], result: { metrics: { test: { rmse: "0.0123", mae: null } }, validation: { holdout_reused: true } } } };
      if (path === "/models") return { as_of: at, model };
    });
    render(<MlLabPage />); await screen.findByRole("button", { name: /Open job job-1/ });
    fireEvent.click(screen.getByRole("button", { name: /Open job job-1/ }));
    await screen.findByText("Lease expired"); fireEvent.click(screen.getByRole("button", { name: "Retry job" }));
    await screen.findByText(/0.0123/);
    change("Model name", "Archive model"); fireEvent.click(screen.getByRole("button", { name: "Register model" }));
    await screen.findByText(/Model registered/);
  });

  it("preserves durable queuing while the supervisor is unavailable", async () => {
    server((path) => path === "/workspace" ? { ...workspace, supervisor: { ...workspace.supervisor, available: false } } : undefined);
    render(<MlLabPage />); await screen.findByText(/Research supervisor unavailable/);
    change("Training dataset", "data-1");
    expect((screen.getByRole("button", { name: "Train model" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getAllByText(at, { selector: "time" }).length).toBeGreaterThan(0);
  });
});

describe("versioned Python research", () => {
  it("saves exact source/config then enqueues isolated validation for that revision", async () => {
    server((path, body) => {
      if (path === "/drafts") {
        expect(body?.source).toBe("print('revision')"); expect(body?.config).toEqual({ warmup: 20 });
        return { as_of: at, draft: { ...draft, source: "print('revision')", config: { warmup: 20 } } };
      }
      if (path === "/jobs") {
        expect(body?.kind).toBe("validate_strategy"); expect(body?.inputs).toEqual({ revision_id: "revision-1" });
        return { as_of: at, job: { ...job, kind: "validate_strategy" } };
      }
      if (path === "/jobs/job-1") return { as_of: at, job: { ...job, kind: "validate_strategy" } };
    });
    render(<StrategyNewPage />); await screen.findByLabelText("Python source");
    change("Draft name", "Archive strategy"); change("Python source", "print('revision')"); change("Strategy config JSON", '{"warmup":20}');
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await screen.findByText(/Saved revision 1 ·/);
    fireEvent.click(screen.getByRole("button", { name: "Validate in isolation" }));
    await screen.findByText("job-1");
    change("Python source", "changed after enqueue");
    expect((screen.getByRole("button", { name: "Validate in isolation" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps unsaved source on a revision conflict and requires an explicit reload", async () => {
    server((path) => {
      if (path === "/drafts/draft-1") return { as_of: at, draft };
      if (path === "/drafts/draft-1/revisions") return new Response(JSON.stringify({ detail: "Revision changed elsewhere" }), { status: 409 });
    });
    render(<StrategyNewPage />); await screen.findByLabelText("Open draft"); change("Open draft", "draft-1");
    await screen.findByDisplayValue("class ArchiveStrategy: pass"); change("Python source", "unsaved owner source");
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await screen.findByRole("alert");
    expect((screen.getByLabelText("Python source") as HTMLTextAreaElement).value).toBe("unsaved owner source");
    expect(screen.getByRole("button", { name: "Reload saved revision" })).toBeTruthy();
  });
});

describe("paper account authority", () => {
  it("requires a visible old/new confirmation, rereads conflicts and never silently retries", async () => {
    let writes = 0;
    server((path, body) => {
      if (path === "/paper/bindings/1") {
        writes++; expect(body).toEqual({ expected: { mode: "off" }, changes: { mode: "trading" } });
        return new Response(JSON.stringify({ detail: "Binding changed elsewhere" }), { status: 409 });
      }
    });
    render(<PaperTradingPage />); await screen.findByLabelText("Binding 1 mode");
    change("Binding 1 mode", "trading"); fireEvent.click(screen.getByRole("button", { name: "Review binding 1" }));
    expect(writes).toBe(0);
    const dialog = screen.getByRole("dialog", { name: "Confirm paper change" });
    expect(dialog.textContent).toContain("off → trading");
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm change" }));
    await screen.findByRole("alert"); expect(writes).toBe(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getAllByText(/Review the refreshed values/).length).toBeGreaterThan(0);
  });

  it("creates only a local paper account after reviewing explicit currency and capital", async () => {
    let submitted: Record<string, unknown> | null = null;
    server((path, body) => { if (path === "/paper/accounts") { submitted = body; return { as_of: at, account }; } });
    render(<PaperTradingPage />); await screen.findByLabelText("Account key");
    change("Account key", "owner-research"); change("Account name", "Research paper"); change("Account currency", "EUR"); change("Initial equity", "100.123456789"); change("Initial cash", "100.123456789");
    fireEvent.click(screen.getByRole("button", { name: "Review account" }));
    expect(submitted).toBeNull(); expect(screen.getByRole("dialog").textContent).toContain("100.123456789 EUR");
    fireEvent.click(screen.getByRole("button", { name: "Confirm change" }));
    await screen.findByText(/Paper account created/);
    expect(submitted).toMatchObject({ base_ccy: "EUR", paper_initial_equity: "100.123456789", paper_initial_cash: "100.123456789" });
  });

  it("keeps paper writes disabled when runtime safety cannot be established", async () => {
    server((path) => path === "/paper" ? { ...paper, safety: { execution_mode: "unknown", allow_live: true, environment: "unknown" } } : undefined);
    render(<PaperTradingPage />); await screen.findByText(/Paper controls are locked/);
    expect((screen.getByRole("button", { name: "Review account" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Review binding 1" }) as HTMLButtonElement).disabled).toBe(true);
  });
});


describe("research boundary recovery", () => {
  it("browses dataset bar pages using the server ordinal cursor", async () => {
    server((path) => {
      if (path.startsWith("/datasets/data-1?")) {
        if (path.includes("after=499")) return { as_of: at, dataset, bars: [{ ts: at, open: "22.123456789", high: "23", low: "22", close: "22.5", volume: "1" }], next_after: null };
        return { as_of: at, dataset, bars: [{ ts: at, open: "11.123456789", high: "12", low: "11", close: "11.5", volume: "1" }], next_after: 499 };
      }
    });
    render(<MarketDataPage />); await screen.findByLabelText("Browse dataset"); change("Browse dataset", "data-1");
    await screen.findByText("11.123456789"); fireEvent.click(screen.getByRole("button", { name: "Next dataset bars" }));
    await screen.findByText("22.123456789"); expect(screen.queryByText("11.123456789")).toBeNull();
  });

  it("keeps cancellation available while a job poll is pending and fences its late result", async () => {
    let resolvePoll!: (value: unknown) => void;
    server((path) => {
      if (path === "/jobs") return { as_of: at, job };
      if (path === "/jobs/job-1") return new Promise((resolve) => { resolvePoll = resolve; });
      if (path === "/jobs/job-1/cancel") return { as_of: at, job: { ...job, status: "cancelled" } };
    });
    render(<MlLabPage />); await screen.findByLabelText("Training dataset"); change("Training dataset", "data-1");
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Train model" })); });
      expect(screen.getByText("job-1")).toBeTruthy();
      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(resolvePoll).toBeTypeOf("function");
      expect((screen.getByRole("button", { name: "Cancel job" }) as HTMLButtonElement).disabled).toBe(false);
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Cancel job" })); });
      await act(async () => { resolvePoll({ as_of: at, job: { ...job, status: "running" } }); });
      expect(screen.getByText("cancelled", { selector: ".status-badge" })).toBeTruthy();
    } finally { vi.useRealTimers(); }
  });

  it("keeps one enqueue request identity across a failed transport retry", async () => {
    const ids: unknown[] = [];
    server((path, body) => {
      if (path === "/jobs") { ids.push(body?.request_id); return ids.length === 1 ? new Response(JSON.stringify({ detail: "Temporarily unavailable" }), { status: 503 }) : { as_of: at, job }; }
    });
    render(<MlLabPage />); await screen.findByLabelText("Training dataset"); change("Training dataset", "data-1");
    fireEvent.click(screen.getByRole("button", { name: "Train model" })); await screen.findByText("Temporarily unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Train model" })); await screen.findByText("job-1");
    expect(ids).toHaveLength(2); expect(ids[0]).toBe(ids[1]);
  });

  it("queues predictions against the selected immutable model and dataset", async () => {
    server((path, body) => {
      if (path === "/jobs") { expect(body?.kind).toBe("predict"); expect(body?.inputs).toEqual({ model_id: "model-1", dataset_id: "data-1", evaluation_start: "2026-10-28T00:00:00Z" }); return { as_of: at, job: { ...job, kind: "predict" } }; }
    });
    render(<MlLabPage />); await screen.findByLabelText("Prediction model"); change("Prediction model", "model-1"); change("Prediction dataset", "data-1"); change("Prediction evaluation start", "2026-10-28T00:00:00Z");
    fireEvent.click(screen.getByRole("button", { name: "Run predictions" })); await screen.findByText("job-1");
  });

  it("displays forecast baselines and undefined metrics without inventing a zero", async () => {
    server((path) => path === "/workspace" ? { ...workspace, jobs: [{ ...job, status: "completed" }] } : path === "/jobs/job-1" ? { as_of: at, job: { ...job, status: "completed", result: { metrics: { model: { count: 240, mae: null, rmse: 0.0123, directional_accuracy: 0.6 }, zero_return: { count: 240, mae: 0.02, rmse: 0.03, directional_accuracy: null }, training_mean: { count: 240, mae: 0.02, rmse: 0.03, directional_accuracy: 0.5 } } } } } : undefined);
    render(<MlLabPage />); await screen.findByRole("button", { name: /Open job job-1/ }); fireEvent.click(screen.getByRole("button", { name: /Open job job-1/ }));
    const row = await screen.findByRole("row", { name: /Fitted model/ });
    expect(within(row).getByText("Undefined")).toBeTruthy(); expect(within(row).getByText("0.0123")).toBeTruthy();
    expect(screen.getByRole("row", { name: /Zero-return baseline/ })).toBeTruthy();
  });

  it("freezes an exact stored window and preserves explicit timestamp offsets", async () => {
    server((path, body) => path === "/datasets/snapshot" ? (expect(body).toEqual({ metadata: { ...metadata, start: "2026-01-01T01:00:00+01:00", end: "2026-02-01T00:00:00Z" }, name: "Recorded window" }), { as_of: at, dataset }) : undefined);
    render(<MarketDataPage />); await screen.findByLabelText("Import instrument"); change("Import instrument", "1"); change("Import source", "recorded-feed"); change("Import currency", "EUR"); change("Import timeframe", "1d"); change("Import provenance", "Owner archive"); change("Dataset name", "Recorded window"); change("Snapshot start", "2026-01-01T01:00:00+01:00"); change("Snapshot end", "2026-02-01T00:00:00Z");
    fireEvent.click(screen.getByRole("button", { name: "Freeze stored window" })); await screen.findByText(/Dataset frozen/);
  });

  it("shows API failure with retry and keeps a maintenance workspace read only", async () => {
    let failed = true;
    server((path) => path === "/workspace" ? failed ? new Response(JSON.stringify({ detail: "Research read unavailable" }), { status: 503 }) : { ...workspace, supervisor: { ...workspace.supervisor, maintenance: true } } : undefined);
    render(<MlLabPage />); await screen.findByRole("alert"); failed = false; fireEvent.click(screen.getByRole("button", { name: "Refresh workspace" })); await screen.findByText("Research maintenance in progress");
    change("Training dataset", "data-1"); expect((screen.getByRole("button", { name: "Train model" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("trusted historical execution", () => {
  it("sends saved revision, immutable dataset/model and exact research assumptions", async () => {
    server((path, body) => {
      if (path === "/drafts/draft-1") return { as_of: at, draft };
      if (path === "/jobs") {
        expect(body?.kind).toBe("backtest");
        expect(body?.inputs).toEqual({ revision_id: "revision-1", dataset_id: "data-1", model_id: "model-1", evaluation_start: "2026-10-28T00:00:00Z", assumptions: { initial_capital: "100.123456789", currency: "EUR", size_pct: "0.25", fee_bps: "2.1", slippage_bps: "1.2", annualization_factor: "252" } });
        return { as_of: at, job: { ...job, kind: "backtest" } };
      }
    });
    render(<StrategyNewPage />); await screen.findByLabelText("Open draft"); change("Open draft", "draft-1"); await screen.findByDisplayValue(draft.source);
    change("Backtest dataset", "data-1"); change("Backtest model", "model-1"); change("Backtest evaluation start", "2026-10-28T00:00:00Z");
    change("Research initial capital", "100.123456789"); change("Position fraction (0–1)", "0.25"); change("Research fee (bps)", "2.1"); change("Research slippage (bps)", "1.2"); change("Annualization factor", "252");
    fireEvent.click(screen.getByRole("button", { name: "Run historical backtest" })); await screen.findByText("job-1");
  });

  it("keeps trusted currency, selection blockers and unliquidated positions visible", async () => {
    const completed = { ...job, kind: "backtest", status: "completed", result: { currency: "EUR", initial_capital: "100.123456789", final_equity: "102.123456789", total_trades: 0, sharpe_ratio: null, selection_eligible: false, selection_blockers: ["unresolved_terminal_position"], terminal_position: { symbol: "ARCHIVE", quantity: "1.23456789" }, cost_breakdown: { commission: "0.02" }, trades: [], equity_curve: [] } };
    server((path) => path === "/workspace" ? { ...workspace, jobs: [completed] } : path === "/jobs/job-1" ? { as_of: at, job: completed } : undefined);
    render(<StrategyNewPage />); await screen.findByRole("button", { name: /Open job job-1/ }); fireEvent.click(screen.getByRole("button", { name: /Open job job-1/ }));
    await screen.findByText("EUR 102.123456789");
    expect(screen.getByText("Ineligible for selection")).toBeTruthy(); expect(screen.getByText("unresolved_terminal_position", { selector: "li" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Unliquidated terminal position" })).toBeTruthy();
    expect(screen.getByText(/No closed trades recorded/)).toBeTruthy();
  });

  it("blocks a dataset with an explicit timing refusal while preserving its diagnostic", async () => {
    server((path) => path === "/workspace" ? { ...workspace, datasets: [{ ...dataset, eligibility: { training: false, backtest: false, training_refusals: ["Authoritative calendar missing"], backtest_refusals: ["Authoritative calendar missing"] } }] } : undefined);
    render(<MlLabPage />); await screen.findByLabelText("Training dataset"); change("Training dataset", "data-1");
    expect((screen.getByRole("button", { name: "Train model" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Authoritative calendar missing", { selector: "li" })).toBeTruthy();
  });
});

describe("source and authority safeguards", () => {
  it("preserves the original UTF-8 BOM in the validation receipt request", async () => {
    server((path, body) => {
      if (path === "/imports") { expect(String(body?.csv).charCodeAt(0)).toBe(0xfeff); return { as_of: at, import: { import_id: "bom-1", content_hash: "bom-hash", valid: false, metadata, report: { errors: ["Unexpected BOM header"], row_count: 0 } } }; }
    });
    render(<MarketDataPage />); await screen.findByLabelText("CSV file"); change("Import instrument", "1"); change("Import source", "recorded-feed"); change("Import currency", "EUR"); change("Import timeframe", "1d"); change("Import provenance", "Owner archive");
    fireEvent.change(screen.getByLabelText("CSV file"), { target: { files: [new File([new Uint8Array([239, 187, 191, 116, 115])], "bom.csv")] } });
    fireEvent.click(screen.getByRole("button", { name: "Validate CSV" })); await screen.findByText(/Unexpected BOM header/);
  });

  it("refuses nonfinite JSON config instead of silently saving null parameters", async () => {
    let saves = 0;
    server((path) => { if (path === "/drafts") { saves++; return { as_of: at, draft }; } });
    render(<StrategyNewPage />); await screen.findByLabelText("Python source"); change("Draft name", "Finite config"); change("Python source", "class UserStrategy: pass"); change("Strategy config JSON", '{"class_name":"UserStrategy","strategy_id":"finite_config","parameters":{"threshold":1e999}}');
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await screen.findByText(/Config numbers must be finite/); expect(saves).toBe(0);
  });

  it("confirms risk with the precise stored expected value and leaves unrelated fields out", async () => {
    let updates = 0;
    server((path, body) => {
      if (path === "/paper/bindings/1") { updates++; expect(body).toEqual({ expected: { max_position_pct: "0.1" }, changes: { max_position_pct: "0.000123456789" } }); return { as_of: at }; }
    });
    render(<PaperTradingPage />); await screen.findByLabelText("Binding 1 maximum position fraction"); change("Binding 1 maximum position fraction", "0.000123456789"); fireEvent.click(screen.getByRole("button", { name: "Review binding 1" }));
    const dialog = screen.getByRole("dialog"); expect(dialog.textContent).toContain("0.1 → 0.000123456789"); expect(updates).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Confirm change" })); await screen.findByText(/Paper binding 1 updated/); expect(updates).toBe(1);
  });

  it("closes a confirmation with Escape before any authority request", async () => {
    let writes = 0;
    server((path) => { if (path.startsWith("/paper/bindings/1")) writes++; });
    render(<PaperTradingPage />); await screen.findByLabelText("Binding 1 mode"); change("Binding 1 mode", "close_only"); fireEvent.click(screen.getByRole("button", { name: "Review binding 1" }));
    const dialog = screen.getByRole("dialog"); fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull(); expect(writes).toBe(0);
  });
});

describe("authenticated evidence exports", () => {
  it("downloads the original CSV and manifest using authenticated fetch", async () => {
    const files: string[] = [];
    const OriginalURL = URL;
    vi.stubGlobal("URL", class extends OriginalURL { static createObjectURL() { return "blob:dataset-export"; } static revokeObjectURL() {} });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { files.push(this.download); });
    server((path) => path === "/datasets/data-1/export" ? { as_of: at, original_csv: "ts,open,high,low,close,volume\r\n", manifest: dataset.manifest } : path.startsWith("/datasets/data-1?") ? { as_of: at, dataset, bars: [], next_after: null } : undefined);
    render(<MarketDataPage />); await screen.findByLabelText("Browse dataset"); change("Browse dataset", "data-1");
    fireEvent.click(screen.getByRole("button", { name: "Export dataset" })); await waitFor(() => expect(files).toEqual(["dataset-data-1.json"]));
  });

  it("pages the dataset catalogue without making older evidence unreachable", async () => {
    server((path) => {
      if (path === "/datasets") return { as_of: at, datasets: [dataset], next_after: "opaque-next" };
      if (path === "/datasets?after=opaque-next") return { as_of: at, datasets: [{ ...dataset, dataset_id: "older-data", name: "Older archive" }], next_after: null };
    });
    render(<MarketDataPage />); await screen.findByLabelText("Browse dataset"); fireEvent.click(screen.getByRole("button", { name: "Older datasets" }));
    await screen.findByRole("option", { name: /Older archive/ }); expect(screen.queryByRole("option", { name: /Recorded archive/ })).toBeNull();
  });
});

describe("retained research history", () => {
  it("loads further dataset, model and job pages into usable selectors and history", async () => {
    server((path) => {
      if (path === "/workspace") return { ...workspace, next_after: { datasets: "data-cursor", models: "model-cursor", jobs: "job-cursor", drafts: null } };
      if (path === "/datasets?after=data-cursor") return { as_of: at, datasets: [{ ...dataset, dataset_id: "older-data", name: "Older archive" }], next_after: null };
      if (path === "/models?after=model-cursor") return { as_of: at, models: [{ ...model, model_id: "older-model", name: "Older model" }], next_after: null };
      if (path === "/jobs?after=job-cursor") return { as_of: at, jobs: [{ ...job, job_id: "older-job", status: "failed" }], next_after: null };
    });
    render(<MlLabPage />); await screen.findByLabelText("Training dataset"); fireEvent.click(screen.getByRole("button", { name: "Load more datasets" }));
    await screen.findByRole("option", { name: /Older archive ·/ }); change("Training dataset", "older-data");
    fireEvent.click(screen.getByRole("button", { name: "Load more models" })); await screen.findByRole("option", { name: "Older model" });
    fireEvent.click(screen.getByRole("button", { name: "Load more jobs" })); await screen.findByRole("button", { name: "Open job older-job" });
    expect((screen.getByLabelText("Training dataset") as HTMLSelectElement).value).toBe("older-data");
  });

  it("loads a retained draft summary and retrieves its exact revision source", async () => {
    server((path) => {
      if (path === "/workspace") return { ...workspace, next_after: { drafts: "draft-cursor" } };
      if (path === "/drafts?after=draft-cursor") return { as_of: at, drafts: [{ draft_id: "old-draft", revision_id: "old-revision", revision: 2, name: "Older strategy", source_hash: "older-hash" }], next_after: null };
      if (path === "/drafts/old-draft") return { as_of: at, draft: { ...draft, draft_id: "old-draft", revision_id: "old-revision", revision: 2, name: "Older strategy", source: "class UserStrategy: pass" } };
    });
    render(<StrategyNewPage />); await screen.findByLabelText("Open draft"); fireEvent.click(screen.getByRole("button", { name: "Load more drafts" })); await screen.findByRole("option", { name: /Older strategy/ }); change("Open draft", "old-draft");
    await screen.findByDisplayValue("class UserStrategy: pass");
  });
});

it("keeps exact quote currency and explicit bar timing beside dataset OHLCV", async () => {
  server((path) => path.startsWith("/datasets/data-1?") ? { as_of: at, dataset, bars: [{ ts: at, open: "8.123456789", high: "9", low: "8", close: "8.5", volume: "1", bar_open: "2026-10-27T12:00:00Z", bar_close: "2026-10-27T12:05:00Z" }], next_after: null } : undefined);
  render(<MarketDataPage />); await screen.findByLabelText("Browse dataset"); change("Browse dataset", "data-1");
  const row = await screen.findByRole("row", { name: /8.123456789/ });
  expect(within(row).getByText("EUR")).toBeTruthy();
  expect(within(row).getByText("2026-10-27T12:05:00Z").getAttribute("datetime")).toBe("2026-10-27T12:05:00Z");
});

it("distinguishes unavailable backtest evidence from known empty results", async () => {
  const completed = { ...job, kind: "backtest", status: "completed", result: { currency: "EUR", final_equity: "100", total_trades: 0, sharpe_ratio: null } };
  server((path) => path === "/workspace" ? { ...workspace, jobs: [completed] } : path === "/jobs/job-1" ? { as_of: at, job: completed } : undefined);
  render(<StrategyNewPage />); await screen.findByRole("button", { name: "Open job job-1" }); fireEvent.click(screen.getByRole("button", { name: "Open job job-1" }));
  await screen.findByText("Closed trade detail unavailable."); expect(screen.queryByText(/No closed trades recorded/)).toBeNull();
  expect(screen.getByText("Selection eligibility unavailable")).toBeTruthy();
});

const templates = [
  { template_id: "moving_average", name: "Moving-average research strategy", source: "# canonical moving-average source\nclass UserStrategy: pass", requires_model: false, config: { class_name: "UserStrategy", strategy_id: "research_ma_v1", parameters: {} } },
  { template_id: "return_model", name: "Registered return-model strategy", source: "# canonical return-model source\nclass UserStrategy: pass", requires_model: true, config: { class_name: "UserStrategy", strategy_id: "research_return_v1", parameters: { return_threshold: 0 } } },
];
describe("canonical research templates", () => {
  it("loads the exact authenticated template source/config into an empty editor", async () => {
    server((path) => path === "/templates" ? { as_of: at, templates } : undefined);
    render(<StrategyNewPage />); await screen.findByLabelText("Strategy template"); change("Strategy template", "moving_average");
    fireEvent.click(screen.getByRole("button", { name: "Load template" }));
    expect((screen.getByLabelText("Python source") as HTMLTextAreaElement).value).toBe(templates[0].source);
    expect(JSON.parse((screen.getByLabelText("Strategy config JSON") as HTMLTextAreaElement).value)).toEqual(templates[0].config);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("preserves edited source until replacement is explicitly confirmed", async () => {
    server((path) => path === "/templates" ? { as_of: at, templates } : undefined);
    render(<StrategyNewPage />); await screen.findByLabelText("Python source"); change("Draft name", "Owner work"); change("Python source", "owner unsaved source");
    change("Strategy template", "return_model"); fireEvent.click(screen.getByRole("button", { name: "Load template" }));
    const dialog = screen.getByRole("dialog", { name: "Replace editor contents?" });
    expect((screen.getByLabelText("Python source") as HTMLTextAreaElement).value).toBe("owner unsaved source");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect((screen.getByLabelText("Python source") as HTMLTextAreaElement).value).toBe("owner unsaved source");
    fireEvent.click(screen.getByRole("button", { name: "Load template" })); fireEvent.click(screen.getByRole("button", { name: "Replace editor contents" }));
    expect((screen.getByLabelText("Python source") as HTMLTextAreaElement).value).toBe(templates[1].source);
    expect((screen.getByLabelText("Draft name") as HTMLInputElement).value).toBe("Owner work");
    expect(screen.getByText(/This template requires a registered model/)).toBeTruthy();
  });

  it("requires a selected model for the model template and sends only its immutable ID", async () => {
    server((path, body) => {
      if (path === "/templates") return { as_of: at, templates };
      if (path === "/drafts") { expect(body?.config).toEqual(templates[1].config); return { as_of: at, draft: { ...draft, source: templates[1].source, config: templates[1].config } }; }
      if (path === "/jobs") { expect(body?.inputs).toEqual({ revision_id: "revision-1", dataset_id: "data-1", model_id: "model-1", assumptions: { initial_capital: "100", currency: "EUR", size_pct: "1", fee_bps: "0", slippage_bps: "0", annualization_factor: "252" } }); return { as_of: at, job: { ...job, kind: "backtest" } }; }
    });
    render(<StrategyNewPage />); await screen.findByLabelText("Strategy template"); change("Strategy template", "return_model"); fireEvent.click(screen.getByRole("button", { name: "Load template" }));
    fireEvent.click(screen.getByRole("button", { name: "Save revision" })); await screen.findByText(/Saved revision 1 ·/);
    change("Backtest dataset", "data-1"); change("Research initial capital", "100"); change("Position fraction (0–1)", "1"); change("Research fee (bps)", "0"); change("Research slippage (bps)", "0"); change("Annualization factor", "252");
    expect((screen.getByRole("button", { name: "Run historical backtest" }) as HTMLButtonElement).disabled).toBe(true);
    change("Backtest model", "model-1"); fireEvent.click(screen.getByRole("button", { name: "Run historical backtest" })); await screen.findByText("job-1");
  });

  it("retries unavailable template reads while keeping manual editing usable", async () => {
    let unavailable = true;
    server((path) => path === "/templates" ? unavailable ? new Response(JSON.stringify({ detail: "Template catalogue unavailable" }), { status: 503 }) : { as_of: at, templates } : undefined);
    render(<StrategyNewPage />); await screen.findByText("Template catalogue unavailable"); change("Python source", "owner manual source"); unavailable = false;
    fireEvent.click(screen.getByRole("button", { name: "Refresh templates" })); await screen.findByRole("option", { name: "Moving-average research strategy" });
    expect((screen.getByLabelText("Python source") as HTMLTextAreaElement).value).toBe("owner manual source");
  });
});

it("shows a retained invalid snapshot receipt instead of reporting a frozen dataset", async () => {
  server((path) => path === "/datasets/snapshot" ? { as_of: at, dataset: null, import: { import_id: "failed-snapshot", valid: false, content_hash: null, original_hash: "original-hash", metadata, created_at: at, report: { errors: ["Stored bar has invalid OHLCV"], row_count: 1 } } } : undefined);
  render(<MarketDataPage />); await screen.findByLabelText("Import instrument"); change("Import instrument", "1"); change("Import source", "recorded-feed"); change("Import currency", "EUR"); change("Import timeframe", "1d"); change("Import provenance", "Owner archive"); change("Dataset name", "Invalid window"); change("Snapshot start", "2026-01-01T00:00:00Z"); change("Snapshot end", "2026-02-01T00:00:00Z");
  fireEvent.click(screen.getByRole("button", { name: "Freeze stored window" })); await screen.findByText("Invalid snapshot receipt");
  expect(screen.getByText(/Stored bar has invalid OHLCV/)).toBeTruthy(); expect(screen.getByText("failed-snapshot")).toBeTruthy();
  expect(screen.queryByText(/Dataset frozen/)).toBeNull(); expect((screen.getByRole("button", { name: "Freeze dataset" }) as HTMLButtonElement).disabled).toBe(true);
});

it("recognizes a reopened model template when the catalogue arrives after its draft", async () => {
  let completeTemplates!: (value: unknown) => void;
  server((path) => {
    if (path === "/templates") return new Promise((resolve) => { completeTemplates = resolve; });
    if (path === "/drafts/draft-1") return { as_of: at, draft: { ...draft, source: templates[1].source, config: templates[1].config } };
  });
  render(<StrategyNewPage />); await screen.findByLabelText("Open draft"); change("Open draft", "draft-1"); await waitFor(() => expect((screen.getByLabelText("Python source") as HTMLTextAreaElement).value).toBe(templates[1].source));
  change("Backtest dataset", "data-1"); change("Research initial capital", "100"); change("Position fraction (0–1)", "1"); change("Research fee (bps)", "0"); change("Research slippage (bps)", "0"); change("Annualization factor", "252");
  await act(async () => { completeTemplates({ as_of: at, templates }); });
  expect((screen.getByRole("button", { name: "Run historical backtest" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/This template requires a registered model/)).toBeTruthy();
});

it("keeps model artifacts out of draft config and requires the registry selector", async () => {
  let saves = 0;
  server((path) => { if (path === "/drafts") { saves++; return { as_of: at, draft }; } });
  render(<StrategyNewPage />); await screen.findByLabelText("Python source"); change("Draft name", "Registered model only"); change("Python source", "class UserStrategy: pass"); change("Strategy config JSON", '{"class_name":"UserStrategy","strategy_id":"registered_only","parameters":{"research_model":{}}}');
  fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
  await screen.findByText(/research_model is supplied by the trusted runner/); expect(saves).toBe(0);
});

const readinessChecks = [
  { key: "release", state: "ok", label: "Released", detail: "Released for trading.", at: null },
  { key: "mode", state: "ok", label: "Mode", detail: "Trading: it opens and closes positions on its own.", at: null },
  { key: "account", state: "ok", label: "Account", detail: "The account is connected.", at: null },
  { key: "worker", state: "ok", label: "Signal worker", detail: "It is processing market data.", at },
  { key: "decision", state: "warn", label: "Last decision", detail: "Scoring skipped its last signal: asset score below threshold", at },
  { key: "risk", state: "ok", label: "Risk limits", detail: "No risk rule blocked this account in the last 24 hours.", at: null },
];
function paperReadiness(readiness: unknown, accountId = 1) {
  return { ...paper, control: { ...paper.control, bindings: [{ ...binding, mode: "trading" }] }, strategies: { as_of: at, strategies: [{ strategy_id: "released-1", name: "Released strategy", asset_class: "equity", description: "Recorded strategy", released: true, version: "1.0.0", status: "active", last_signal: null, realized_pnl: [], bindings: [{ binding_id: 1, account_id: accountId, account_name: "Local paper", mode: "trading", active: true, autopilot: true, entries_enabled: true, exits_enabled: true, readiness }] }] } };
}
describe("recorded paper binding readiness", () => {
  it("shows the server ready verdict, warnings and exact worker observation time", async () => {
    server((path) => path === "/paper" ? paperReadiness({ ready: true, checks: readinessChecks }) : undefined);
    render(<PaperTradingPage />);
    const region = await screen.findByRole("region", { name: "Binding 1 readiness" });
    expect(within(region).getByText("Ready").className).toContain("success");
    expect(within(region).getByText("Warning").className).toContain("warning");
    expect(within(region).getByText("Scoring skipped its last signal: asset score below threshold")).toBeTruthy();
    expect(within(region).getAllByText(at, { selector: "time" }).every((time) => time.getAttribute("datetime") === at)).toBe(true);
    expect(within(region).getByRole("list").children).toHaveLength(6);
  });

  it("shows blocked reasons and unknown history without recalculating readiness", async () => {
    const checks = readinessChecks.map((check) => check.key === "worker" ? { ...check, state: "blocked", detail: "No signal worker has run this strategy here. STRATEGY_LIST is required.", at: null } : check.key === "decision" ? { ...check, state: "unknown", detail: "No decision in the last 7 days.", at: null } : check);
    server((path) => path === "/paper" ? paperReadiness({ ready: false, checks }) : undefined);
    render(<PaperTradingPage />);
    const region = await screen.findByRole("region", { name: "Binding 1 readiness" });
    expect(within(region).getAllByText("Blocked").every((badge) => badge.className.includes("danger"))).toBe(true);
    expect(within(region).getByText(/STRATEGY_LIST is required/)).toBeTruthy();
    expect(within(region).getByText("Unknown").className).toContain("neutral");
    expect(within(region).getByText("No decision in the last 7 days.")).toBeTruthy();
    expect(within(region).queryByText("Ready")).toBeNull();
  });

  it.each(["null", "omitted", "other-account", "section-unavailable"])("keeps %s readiness unavailable instead of borrowing or inventing health", async (scenario) => {
    server((path) => {
      if (path !== "/paper") return undefined;
      if (scenario === "section-unavailable") return { ...paper, strategies: null };
      return paperReadiness(scenario === "null" ? null : scenario === "omitted" ? undefined : { ready: true, checks: readinessChecks }, scenario === "other-account" ? 2 : 1);
    });
    render(<PaperTradingPage />);
    const region = await screen.findByRole("region", { name: "Binding 1 readiness" });
    expect(within(region).getByText("Readiness unavailable")).toBeTruthy();
    expect(within(region).queryByText("Ready")).toBeNull();
    expect(within(region).queryByText("It is processing market data.")).toBeNull();
    expect(screen.getByLabelText("Binding 1 mode")).toBeTruthy();
  });
});

describe("model evaluation cutoff labels", () => {
  it("identifies the registered timestamp as the inclusive earliest allowed evaluation time", async () => {
    const evaluationFrom = "2026-10-28T08:30:00.123456Z";
    server((path) => path === "/workspace" ? { ...workspace, models: [{ ...model, training_cutoff: evaluationFrom }] } : undefined);
    render(<MlLabPage />);
    const label = await screen.findByText(/Earliest allowed evaluation:/);
    expect(within(label).getByText(evaluationFrom).getAttribute("datetime")).toBe(evaluationFrom);
    expect(screen.getByText(/Evaluation may start at or after the model's earliest allowed evaluation time/)).toBeTruthy();
    expect(screen.queryByText(/Training-label cutoff:/)).toBeNull();
  });

  it("uses the same earliest evaluation meaning in the backtest model selector", async () => {
    const evaluationFrom = "2026-10-28T08:30:00.123456Z";
    server((path) => path === "/workspace" ? { ...workspace, models: [{ ...model, training_cutoff: evaluationFrom }] } : undefined);
    render(<StrategyNewPage />);
    const selector = await screen.findByLabelText("Backtest model");
    expect(within(selector).getByRole("option", { name: `Archive model · earliest evaluation ${evaluationFrom}` })).toBeTruthy();
  });
});
