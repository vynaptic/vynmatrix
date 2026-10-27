import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PnLPage from "@/app/(workspace)/pnl/page";
import type { PnLData } from "@/lib/types";

const asOf = "2026-10-27T12:00:00Z";
const account: PnLData["accounts"][number] = {
  account_id: 1, name: "Euro account", currency: "EUR", starting_equity: "1000",
  equity: { value: "1123.45", as_of: asOf, source: "ledger" },
  equity_daily: [{ date: "2026-10-01", value: "1012.34567890123456789" }],
  equity_by_trade: [], realized_total: "123.45", realized: [], positions: [], fees: [],
};

function response(days: number, overrides: Partial<typeof account> = {}): Response {
  return Response.json({ as_of: asOf, days, accounts: [{ ...account, ...overrides }] });
}

function pendingResponse() {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe("P&L historical windows", () => {
  it("renders the requested daily history and separates current and cumulative figures", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const days = new URL(url, "http://localhost").searchParams.get("days");
      if (days === "30") return Promise.resolve(response(30, {
        equity_daily: [{ date: "2026-10-20", value: "1100.1" }],
      }));
      if (days === "180") return Promise.resolve(response(180, {
        equity_daily: [{ date: "2026-05-01", value: "999.9" }],
      }));
      return Promise.resolve(response(90));
    }));
    render(<PnLPage />);

    const history = await screen.findByRole("table", { name: /Euro account daily equity.*90 days/ });
    expect(within(history).getByRole("row", { name: "2026-10-01 EUR 1,012.34567890123456789" })).toBeTruthy();
    expect(screen.getByText("Latest recorded equity")).toBeTruthy();
    expect(screen.getByText("Cumulative realized P&L")).toBeTruthy();
    expect(screen.getByText("Current open positions")).toBeTruthy();
    expect(screen.getByText("EUR 1,123.45")).toBeTruthy();
    expect(screen.getByText("+EUR 123.45")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    await screen.findByRole("row", { name: "2026-10-20 EUR 1,100.1" });
    expect(screen.queryByText("2026-10-01")).toBeNull();
    expect(screen.getByRole("table", { name: /Euro account daily equity.*30 days/ })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "180 days" }));
    await screen.findByRole("row", { name: "2026-05-01 EUR 999.9" });
    expect(screen.queryByText("2026-10-20")).toBeNull();
    expect(screen.getByRole("table", { name: /Euro account daily equity.*180 days/ })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "90 days" }));
    await screen.findByRole("row", { name: "2026-10-01 EUR 1,012.34567890123456789" });
    expect(screen.queryByText("2026-05-01")).toBeNull();
  });

  it("clears old history while loading and keeps the latest request when responses arrive out of order", async () => {
    const oldRequest = pendingResponse();
    const latestRequest = pendingResponse();
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response(90))
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(latestRequest.promise));
    render(<PnLPage />);
    await screen.findByRole("heading", { name: "Euro account" });

    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    expect(screen.getByRole("status").textContent).toContain("Loading P&L data");
    expect(screen.queryByRole("heading", { name: "Euro account" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "180 days" }));
    await act(async () => latestRequest.resolve(response(180, {
      equity_daily: [{ date: "2026-05-01", value: "999.9" }],
    })));
    await screen.findByRole("row", { name: "2026-05-01 EUR 999.9" });

    await act(async () => oldRequest.resolve(response(30, {
      equity_daily: [{ date: "2026-10-20", value: "1100.1" }],
    })));
    expect(screen.getByRole("row", { name: "2026-05-01 EUR 999.9" })).toBeTruthy();
    expect(screen.queryByText("2026-10-20")).toBeNull();
    expect(screen.getByRole("button", { name: "180 days" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("ignores a stale request failure after the selected period succeeds", async () => {
    const oldRequest = pendingResponse();
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response(90))
      .mockReturnValueOnce(oldRequest.promise)
      .mockResolvedValueOnce(response(180, { equity_daily: [{ date: "2026-05-01", value: "999.9" }] })));
    render(<PnLPage />);
    await screen.findByRole("heading", { name: "Euro account" });
    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    fireEvent.click(screen.getByRole("button", { name: "180 days" }));
    await screen.findByRole("row", { name: "2026-05-01 EUR 999.9" });

    await act(async () => oldRequest.reject(new Error("offline")));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("row", { name: "2026-05-01 EUR 999.9" })).toBeTruthy();
  });

  it("clears failed data and recovers with a different period or a retry of the same period", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response(90))
      .mockResolvedValueOnce(new Response("History service unavailable", { status: 503 }))
      .mockResolvedValueOnce(new Response("History service unavailable", { status: 503 }))
      .mockResolvedValueOnce(response(180, { equity_daily: [{ date: "2026-05-01", value: "999.9" }] })));
    render(<PnLPage />);
    await screen.findByRole("heading", { name: "Euro account" });
    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    expect((await screen.findByRole("alert")).textContent).toContain("History service unavailable");
    expect(screen.queryByRole("heading", { name: "Euro account" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "180 days" }));
    expect((await screen.findByRole("alert")).textContent).toContain("History service unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("row", { name: "2026-05-01 EUR 999.9" });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("table", { name: /Euro account daily equity.*180 days/ })).toBeTruthy();
  });

  it("distinguishes unavailable history and positions from known empty results", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({
      as_of: asOf, days: 90, accounts: [
        { ...account, name: "Unavailable account", equity_daily: null, positions: null },
        { ...account, account_id: 2, name: "Empty account", equity_daily: [], positions: [] },
      ],
    })));
    render(<PnLPage />);
    const missing = (await screen.findByRole("heading", { name: "Unavailable account" })).closest("section")!;
    const empty = screen.getByRole("heading", { name: "Empty account" }).closest("section")!;
    expect(within(missing).getByText("Daily equity history unavailable")).toBeTruthy();
    expect(within(missing).getByText("Positions unavailable")).toBeTruthy();
    expect(within(missing).queryByText("No open positions")).toBeNull();
    expect(within(empty).getByText("No daily equity history in the last 90 days")).toBeTruthy();
    expect(within(empty).getByText("No open positions")).toBeTruthy();
    expect(within(empty).queryByText("Positions unavailable")).toBeNull();
  });
});
