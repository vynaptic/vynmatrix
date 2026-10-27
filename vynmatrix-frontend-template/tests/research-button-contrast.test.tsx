import { readFileSync } from "node:fs";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MarketDataPage from "@/app/(workspace)/market-data/page";
import MlLabPage from "@/app/(workspace)/ml-lab/page";
import StrategyNewPage from "@/app/(workspace)/strategies/new/page";
import PaperTradingPage from "@/app/(workspace)/paper-trading/page";

const at = "2026-10-27T12:34:56Z";
const dataset = { dataset_id: "data-1", name: "Owner archive", content_hash: "content-hash", metadata: { currency: "EUR" }, manifest: {}, bar_count: 1200, created_at: at, eligibility: { training: true, backtest: true } };
const draft = { draft_id: "draft-1", revision_id: "revision-1", revision: 1, name: "Owner strategy", source: "class UserStrategy: pass", config: { class_name: "UserStrategy", strategy_id: "owner_strategy", parameters: {} }, source_hash: "source-hash" };
const job = { job_id: "job-1", kind: "backtest", status: "completed", inputs: {}, created_at: at, started_at: at, finished_at: at, error: null, result: null, attempts: [] };
const workspace = { as_of: at, datasets: [dataset], drafts: [draft], jobs: [job], models: [], supervisor: { available: true, as_of: at, maintenance: false, generation: "generation-1" }, eligibility: {}, limits: {} };
const paper = { as_of: at, safety: { execution_mode: "paper", allow_live: false, environment: "paper" }, control: { as_of: at, owner: null, accounts: [], bindings: [], strategies: [], activity: [], modes: ["off", "close_only", "trading"], limits: {} }, strategies: { as_of: at, strategies: [] }, pnl: { as_of: at, days: 90, accounts: [] }, fills: { as_of: at, fills: [], next_before: null } };
let stylesheet: HTMLStyleElement;
beforeEach(() => {
  stylesheet = document.createElement("style");
  stylesheet.textContent = readFileSync("src/app/globals.css", "utf8");
  document.head.appendChild(stylesheet);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const path = url.replace("/api/ui/research", "");
    const body = path === "/workspace" ? workspace : path === "/templates" ? { as_of: at, templates: [] } : path === "/paper" ? paper : path === "/drafts/draft-1" ? { as_of: at, draft } : path === "/jobs/job-1" ? { as_of: at, job } : path === "/datasets" ? { as_of: at, datasets: [dataset], next_after: null } : path.startsWith("/market?") ? { as_of: at, instruments: [{ instrument_id: 1, symbol: "ARCHIVE", currency: "EUR", asset_class: "equity" }], feeds: [], bars: [], next_before: null } : null;
    if (body === null) throw new Error(`Unexpected read: ${path}`);
    return new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => stylesheet.remove());
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
function rgb(value: string): number[] {
  const parsed = value.match(/^rgb\((\d+), (\d+), (\d+)\)$/);
  expect(parsed, `Expected an explicit opaque color, received ${value}`).not.toBeNull();
  return parsed!.slice(1).map(Number);
}
function luminance(channels: number[]): number {
  const linear = channels.map((channel) => { const fraction = channel / 255; return fraction <= 0.04045 ? fraction / 12.92 : ((fraction + 0.055) / 1.055) ** 2.4; });
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}
function contrast(foreground: string, background: string): number {
  const values = [luminance(rgb(foreground)), luminance(rgb(background))].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}
async function prepare(page: string) {
  if (page === "Market Data") {
    render(<MarketDataPage />); await screen.findByLabelText("CSV file");
    change("Import instrument", "1"); change("Import source", "archive"); change("Import currency", "EUR"); change("Import timeframe", "1d"); change("Import provenance", "Owner archive");
    fireEvent.change(screen.getByLabelText("CSV file"), { target: { files: [new File(["ts,open,high,low,close,volume"], "archive.csv")] } });
    return "Validate CSV";
  }
  if (page === "ML Lab") { render(<MlLabPage />); await screen.findByLabelText("Training dataset"); change("Training dataset", "data-1"); return "Train model"; }
  if (page === "New Strategy") {
    render(<StrategyNewPage />); await screen.findByLabelText("Open draft"); change("Open draft", "draft-1"); await screen.findByDisplayValue("class UserStrategy: pass");
    change("Backtest dataset", "data-1"); change("Research initial capital", "100"); change("Position fraction (0–1)", "1"); change("Research fee (bps)", "0"); change("Research slippage (bps)", "0"); change("Annualization factor", "252");
    fireEvent.click(screen.getByRole("button", { name: "Open job job-1" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Refresh job" }) as HTMLButtonElement).disabled).toBe(false));
    return "Run historical backtest";
  }
  render(<PaperTradingPage />); await screen.findByLabelText("Account key");
  change("Account key", "owner-paper"); change("Account name", "Owner paper"); change("Account currency", "EUR"); change("Initial equity", "100"); change("Initial cash", "100");
  return "Review account";
}
describe("research action readability", () => {
  it.each(["Market Data", "ML Lab", "New Strategy", "Paper Trading"])("keeps enabled %s actions opaque with readable text", async (page) => {
    const primary = await prepare(page);
    expect((screen.getByRole("button", { name: primary }) as HTMLButtonElement).disabled).toBe(false);
    const actions = [...document.querySelectorAll<HTMLButtonElement>("button.button:not([disabled])")];
    expect(actions.length).toBeGreaterThan(1);
    for (const action of actions) {
      const style = getComputedStyle(action);
      expect(contrast(style.color, style.backgroundColor), `${action.textContent} foreground/background contrast`).toBeGreaterThanOrEqual(4.5);
    }
  });
});
