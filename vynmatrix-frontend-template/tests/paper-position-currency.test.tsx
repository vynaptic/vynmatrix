import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PaperTradingPage from "@/app/(workspace)/paper-trading/page";
import type { PaperData } from "@/lib/research-types";

const observed = "2026-10-27T12:34:56.123456Z";
const data: PaperData = {
  as_of: observed,
  safety: { execution_mode: "paper", allow_live: false, environment: "paper" },
  control: { as_of: observed, owner: null, accounts: [], bindings: [], strategies: [], activity: [], modes: ["off", "close_only", "trading"], limits: {} },
  strategies: { as_of: observed, strategies: [] },
  fills: { as_of: observed, fills: [], next_before: null },
  pnl: {
    as_of: observed, days: 90, accounts: [{
      account_id: 1, name: "Swiss base account", currency: "CHF", starting_equity: "1000",
      equity: { value: "1000.25", as_of: observed, source: "execution_snapshot" },
      equity_daily: [], equity_by_trade: [], realized_total: "0", realized: [], fees: [],
      // Account-valued notional does not establish the instrument's price currency.
      positions: [
        { symbol: "ABC", quantity: "2", average_price: "123.456789", last_mark: "124.100000001", notional: "248.200000002", notional_currency: "CHF", as_of: observed },
        { symbol: "MISSING", quantity: "1", average_price: null, last_mark: null, notional: null, notional_currency: "CHF", as_of: observed },
      ],
    }],
  },
};

describe("paper position price currency", () => {
  it("keeps price precision and refuses to label prices with account-valued notional currency", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(data), { status: 200 })));
    render(<PaperTradingPage />);
    const position = await screen.findByRole("row", { name: /ABC 2/ });
    expect(within(position).getByText("123.456789 · Quote currency unavailable")).toBeTruthy();
    expect(within(position).getByText("124.100000001 · Quote currency unavailable")).toBeTruthy();
    expect(position.textContent).not.toContain("CHF");
    expect(screen.getByText(/Latest equity: CHF 1,000.25/)).toBeTruthy();
    const missing = screen.getByRole("row", { name: /MISSING 1/ });
    expect(within(missing).getAllByText("Unavailable")).toHaveLength(2);
    expect(missing.textContent).not.toContain("CHF");
  });
});
