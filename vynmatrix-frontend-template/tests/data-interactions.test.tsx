import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import OverviewPage from "@/app/(workspace)/overview/page";
import ActivityPage from "@/app/(workspace)/activity/page";
import StrategiesPage from "@/app/(workspace)/strategies/page";
import type { OverviewData, StrategiesData } from "@/lib/types";

const apiBoundary = vi.hoisted(() => ({ overview: vi.fn(), strategies: vi.fn() }));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api-client")>(),
  api: apiBoundary,
}));

const at = "2026-10-23T13:45:00Z";
const signal = {
  kind: "signal" as const, at, strategy_id: "trend", symbol: "EURUSD",
  action: "long", confidence: "0.8",
};
const fill = {
  kind: "fill" as const, id: 42, at, strategy_id: "trend", symbol: "EURUSD",
  account_id: 1, side: "buy", quantity: "2", price: "1.23450", currency: "USD",
  fee: "0.25", fee_currency: "EUR", venue: "paper",
};

function overview(activity: OverviewData["activity"] = []): OverviewData {
  return {
    as_of: at, safety: { execution_mode: "paper", allow_live: false, environment: "paper" },
    owner: { display_name: "Owner", base_currency: "EUR", timezone: "UTC" },
    accounts: [{
      account_id: 1, name: "Paper account", environment: "paper", currency: "EUR", status: "connected",
      starting_equity: "1000", equity: { value: "1000", as_of: at, source: "ledger" },
      equity_trend: [], realized_pnl: { value: "0", as_of: at }, open_positions: 0, fills: 50,
    }],
    strategies: { catalogue: 2, released: 2, bound: 3, active: 2 },
    freshness: { price_feeds: [], last_signal_at: at, last_fill_at: at }, activity,
  };
}

const strategy = {
  strategy_id: "trend-id", name: "Swing trend", asset_class: "equity", description: "Trend signal",
  released: true, version: "1.0.0", status: "active", last_signal: null,
  bindings: [{
    binding_id: 1, account_id: 1, account_name: "Paper account", mode: "trading",
    active: true, autopilot: false, entries_enabled: true, exits_enabled: true,
  }],
  realized_pnl: [{ account_id: 1, currency: "EUR", value: "2.5" }],
};
const strategies: StrategiesData = { as_of: at, strategies: [
  strategy,
  { ...strategy, strategy_id: "carry-fx", name: "Carry", asset_class: "fx", bindings: [], realized_pnl: [] },
] };

function activityItems(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(".activity-item"));
}

beforeEach(() => vi.clearAllMocks());

describe.each([
  ["overview", OverviewPage], ["activity", ActivityPage],
] as const)("%s activity identity", (_name, Page) => {
  it("renders simultaneous signals and exact duplicates without duplicate keys", async () => {
    const errors = vi.spyOn(console, "error");
    apiBoundary.overview.mockResolvedValue(overview([
      signal, { ...signal, symbol: "GBPUSD" }, { ...signal }, fill,
    ]));
    const { container } = render(<Page />);
    await screen.findByRole("heading", { name: "Recent activity" });

    expect(activityItems(container)).toHaveLength(4);
    expect(errors.mock.calls.filter((args) => String(args[0]).includes("same key"))).toHaveLength(0);
  });

  it("keeps fallback event nodes when unrelated simultaneous events move", async () => {
    const first = { ...signal };
    const second = { ...signal, symbol: "GBPUSD" };
    const data = overview([first, second]);
    apiBoundary.overview.mockResolvedValue(data);
    const { container, rerender } = render(<Page />);
    await screen.findByRole("heading", { name: "Recent activity" });
    const [firstNode, secondNode] = activityItems(container);

    data.activity = [second, { ...signal, symbol: "USDJPY" }, first];
    rerender(<Page />);

    expect(activityItems(container)[0]).toBe(secondNode);
    expect(activityItems(container)[2]).toBe(firstNode);
  });

  it("keeps duplicate occurrences stable when unrelated events are inserted", async () => {
    const data = overview([{ ...signal }, { ...signal }]);
    apiBoundary.overview.mockResolvedValue(data);
    const { container, rerender } = render(<Page />);
    await screen.findByRole("heading", { name: "Recent activity" });
    const [firstDuplicate, secondDuplicate] = activityItems(container);

    data.activity = [{ ...signal, symbol: "USDJPY" }, ...data.activity];
    rerender(<Page />);

    expect(activityItems(container)[1]).toBe(firstDuplicate);
    expect(activityItems(container)[2]).toBe(secondDuplicate);
  });

  it("uses the persisted fill id when its observable fields change", async () => {
    const data = overview([{ ...fill }]);
    apiBoundary.overview.mockResolvedValue(data);
    const { container, rerender } = render(<Page />);
    await screen.findByRole("heading", { name: "Recent activity" });
    const node = activityItems(container)[0];

    data.activity = [{ ...fill, at: "2026-10-23T13:46:00Z", price: "1.25" }];
    rerender(<Page />);

    expect(activityItems(container)[0]).toBe(node);
    expect(node.textContent).toContain("1.25");
  });

  it("retains fill price currency", async () => {
    apiBoundary.overview.mockResolvedValue(overview([fill]));
    const { container } = render(<Page />);
    await screen.findByRole("heading", { name: "Recent activity" });

    expect(activityItems(container)[0].textContent).toContain("@ USD 1.2345");
  });
});

describe("overview backend labels", () => {
  it("marks connected accounts healthy and identifies the active binding count", async () => {
    apiBoundary.overview.mockResolvedValue(overview());
    render(<OverviewPage />);
    await screen.findByRole("heading", { name: /Owner/ });

    expect(screen.getByText("connected").classList.contains("success")).toBe(true);
    const activeBindings = screen.getByText("Active bindings").closest(".metric-card");
    expect(activeBindings?.querySelector(".metric-value")?.textContent).toBe("2");
  });
});

describe("bounded activity counters", () => {
  it("identifies counts as recent even when the account has more lifetime fills", async () => {
    apiBoundary.overview.mockResolvedValue(overview([signal, fill]));
    render(<ActivityPage />);
    await screen.findByRole("heading", { name: "Activity Timeline" });

    const recentEvents = screen.getByText("Recent events").closest(".metric-card");
    expect(recentEvents?.querySelector(".metric-value")?.textContent).toBe("2");
    for (const label of ["Recent signals", "Recent fills"]) {
      expect(screen.getByText(label).closest(".metric-card")?.querySelector(".metric-value")?.textContent).toBe("1");
    }
  });
});

describe("strategy catalogue search", () => {
  it.each(["sWiNg", "TREND-ID", "Equity"])("filters by %s without changing catalogue metrics", async (query) => {
    apiBoundary.strategies.mockResolvedValue(strategies);
    render(<StrategiesPage />);
    await screen.findByRole("heading", { name: "Strategy Lab" });

    fireEvent.change(screen.getByPlaceholderText("Search strategies..."), { target: { value: query } });

    expect(screen.getByRole("row", { name: /Swing trend/ })).toBeTruthy();
    expect(screen.queryByRole("row", { name: /Carry/ })).toBeNull();
    const total = screen.getByText("Total strategies").closest(".metric-card") as HTMLElement;
    expect(within(total).getByText("02")).toBeTruthy();
    const pnl = screen.getByText("Total P&L").closest(".metric-card") as HTMLElement;
    expect(within(pnl).getByText("+EUR 2.5")).toBeTruthy();
  });

  it("shows empty search results and restores rows when the query is cleared", async () => {
    apiBoundary.strategies.mockResolvedValue(strategies);
    render(<StrategiesPage />);
    await screen.findByRole("heading", { name: "Strategy Lab" });
    const search = screen.getByPlaceholderText("Search strategies...");

    fireEvent.change(search, { target: { value: "unmatched" } });
    expect(screen.getByText("No strategies match your search.")).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(2);

    fireEvent.change(search, { target: { value: "" } });
    expect(screen.queryByText("No strategies match your search.")).toBeNull();
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  it("does not offer unsupported row actions", async () => {
    apiBoundary.strategies.mockResolvedValue(strategies);
    render(<StrategiesPage />);
    await screen.findByRole("heading", { name: "Strategy Lab" });

    expect(screen.queryAllByRole("button", { name: /More actions/ })).toHaveLength(0);
    expect(screen.getAllByRole("columnheader")).toHaveLength(6);
  });
});
