import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import OverviewPage from "@/app/(workspace)/overview/page";
import PnLPage from "@/app/(workspace)/pnl/page";
import StrategiesPage from "@/app/(workspace)/strategies/page";

const apiBoundary = vi.hoisted(() => ({
  overview: vi.fn(),
  strategies: vi.fn(),
  pnl: vi.fn(),
}));

vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api-client")>(),
  api: apiBoundary,
}));

const asOf = "2026-10-26T12:00:00Z";
const binding = (binding_id: number, mode: string, active: boolean) => ({
  binding_id, account_id: binding_id, account_name: "Paper account", mode, active,
  autopilot: mode === "trading", entries_enabled: mode === "trading", exits_enabled: active,
  instruments_allowed: null, asset_classes_allowed: ["equity"], readiness: null,
});
const strategy = {
  strategy_id: "trend", name: "Trend", asset_class: "equity", description: "Trend signal",
  released: true, version: "1.0.0", status: "active", last_signal: null,
  bindings: [binding(1, "trading", true), binding(2, "close_only", true), binding(3, "trading", false)],
  realized_pnl: [
    { account_id: 1, currency: "EUR", value: "12.1" },
    { account_id: 2, currency: "USD", value: "7.2" },
  ],
};

function metric(label: string): HTMLElement {
  const card = screen.getByText(label).closest(".metric-card");
  if (!(card instanceof HTMLElement)) throw new Error(`Missing metric: ${label}`);
  return card;
}

beforeEach(() => vi.clearAllMocks());

describe("backend-shaped monetary page responses", () => {
  it("derives strategy counts and counts all active bindings, including close-only", async () => {
    apiBoundary.strategies.mockResolvedValue({ as_of: asOf, strategies: [
      strategy,
      { ...strategy, strategy_id: "draft", name: "Draft strategy", released: false, bindings: [], realized_pnl: [] },
    ] });
    render(<StrategiesPage />);
    await screen.findByRole("heading", { name: "Strategy Lab" });

    expect(metric("Total strategies").textContent).toContain("02");
    expect(within(metric("Total strategies")).getByText("Released: 1")).toBeTruthy();
    expect(within(metric("Catalogue")).getByText("2")).toBeTruthy();
    expect(within(metric("Active bindings")).getByText("02")).toBeTruthy();
  });

  it("keeps mixed currencies separate in both strategy rows and the headline", async () => {
    apiBoundary.strategies.mockResolvedValue({ as_of: asOf, strategies: [
      strategy,
      { ...strategy, strategy_id: "other", name: "Other", bindings: [], realized_pnl: [{ account_id: 1, currency: "EUR", value: "0.2" }] },
    ] });
    render(<StrategiesPage />);
    await screen.findByRole("heading", { name: "Strategy Lab" });

    const row = screen.getByRole("row", { name: /Trend\s*trend/ });
    expect(row.textContent).toContain("+EUR 12.1");
    expect(row.textContent).toContain("+USD 7.2");
    expect(metric("Total P&L").textContent).toContain("+EUR 12.3");
    expect(metric("Total P&L").textContent).toContain("+USD 7.2");
  });

  it("renders unavailable strategy metrics without crashing or inventing zero", async () => {
    apiBoundary.strategies.mockResolvedValue({ as_of: asOf, strategies: [
      { ...strategy, realized_pnl: null },
      { ...strategy, strategy_id: "known", name: "Known strategy" },
    ] });
    render(<StrategiesPage />);
    await screen.findByRole("heading", { name: "Strategy Lab" });

    expect(metric("Total P&L").textContent).toContain("Unavailable");
    expect(metric("Total P&L").textContent).not.toContain("+EUR 12.1");
    expect(screen.getByRole("row", { name: /Trend\s*trend/ }).textContent).toContain("Unavailable");
    expect(document.body.textContent).not.toContain("NaN");
  });

  it("labels each overview account and sums equity only within its currency", async () => {
    const account = {
      account_id: 1, name: "Euro account", environment: "paper", currency: "EUR", status: "active",
      starting_equity: "10", equity: { value: "12.1", as_of: asOf, source: "ledger" },
      equity_trend: [], realized_pnl: { value: "2.1", as_of: asOf }, open_positions: 0, fills: 0,
    };
    apiBoundary.overview.mockResolvedValue({
      as_of: asOf, safety: { execution_mode: "paper", allow_live: false, environment: "paper" },
      owner: { display_name: "Owner", base_currency: "EUR", timezone: "UTC" },
      accounts: [
        account,
        { ...account, account_id: 2, name: "Second euro account", equity: { ...account.equity, value: "0.2" } },
        { ...account, account_id: 3, name: "Dollar account", currency: "USD", equity: { ...account.equity, value: "7.2" } },
        { ...account, account_id: 4, name: "Missing account", currency: "GBP", equity: null, fills: null, open_positions: null },
      ],
      strategies: { catalogue: 1, released: 1, bound: 1, active: 1 },
      freshness: { price_feeds: [], last_signal_at: null, last_fill_at: null }, activity: [],
    });
    render(<OverviewPage />);
    await screen.findByRole("heading", { name: /Owner/ });

    expect(metric("Total equity").textContent).toContain("EUR 12.3");
    expect(metric("Total equity").textContent).toContain("USD 7.2");
    expect(metric("Total equity").textContent).toContain("GBP Unavailable");
    const euroAccount = screen.getByText("Euro account").closest(".strategy-health-item");
    expect(euroAccount?.textContent).toContain("EUR 12.1");
    expect(screen.getByText("Missing account").closest(".strategy-health-item")?.textContent).toContain("Unavailable");
    expect(metric("Open positions").textContent).toContain("Unavailable");
    expect(metric("Total fills").textContent).toContain("Unavailable");
    expect(document.body.textContent).not.toContain("$");
  });

  it("shows unavailable overview strategy counts when that backend section fails", async () => {
    apiBoundary.overview.mockResolvedValue({
      as_of: asOf, safety: { execution_mode: "paper", allow_live: false, environment: "paper" },
      owner: { display_name: "Owner", base_currency: "EUR", timezone: "UTC" },
      accounts: [], strategies: null,
      freshness: { price_feeds: [], last_signal_at: null, last_fill_at: null }, activity: [],
    });
    render(<OverviewPage />);
    await screen.findByRole("heading", { name: /Owner/ });

    const strategies = metric("Active strategies");
    expect(strategies.querySelector(".metric-value")?.textContent).toBe("Unavailable");
    expect(strategies.querySelector(".metric-change")?.textContent).toBe("Bindings unavailable");
  });

  it("uses account/notional currencies and marks unknown quote currency or missing P&L values", async () => {
    apiBoundary.pnl.mockResolvedValue({ as_of: asOf, days: 90, accounts: [{
      account_id: 1, name: "Euro account", currency: "EUR", starting_equity: "1000",
      equity: { value: "1000.25", as_of: asOf, source: "ledger" }, equity_daily: [], equity_by_trade: [],
      realized_total: "0", realized: [{ strategy_id: "trend", symbol: "ABC", value: "-2.25", as_of: asOf }],
      positions: [
        { symbol: "ABC", quantity: "2", average_price: "123.456", last_mark: "124.1", notional: "248.2", notional_currency: "CHF", as_of: asOf },
        { symbol: "MISSING", quantity: "1", average_price: null, last_mark: null, notional: null, notional_currency: null, as_of: asOf },
      ], fees: [],
    }, {
      account_id: 2, name: "Missing account", currency: "GBP", starting_equity: null,
      equity: null, equity_daily: null, equity_by_trade: null, realized_total: null,
      realized: null, positions: null, fees: null,
    }] });
    render(<PnLPage />);
    await screen.findByRole("heading", { name: "Profit & Loss" });

    expect(screen.getByText("+EUR 0")).toBeTruthy();
    expect(screen.getByText("EUR 1,000.25")).toBeTruthy();
    expect(screen.getByText("-EUR 2.25")).toBeTruthy();
    const position = screen.getByRole("row", { name: /ABC 2/ });
    expect(position.textContent).toContain("123.456 · Quote currency unavailable");
    expect(position.textContent).toContain("124.1 · Quote currency unavailable");
    expect(position.textContent).toContain("CHF 248.2");
    const missing = screen.getByRole("row", { name: /MISSING 1/ });
    expect(within(missing).getAllByText("Unavailable")).toHaveLength(3);
    const missingAccount = screen.getByRole("heading", { name: "Missing account" }).closest("section");
    expect(missingAccount?.textContent).toContain("Unavailable");
    expect(document.body.textContent).not.toContain("NaN");
    expect(document.body.textContent).not.toContain("$");
  });
});
