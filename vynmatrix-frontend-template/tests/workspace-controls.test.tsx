import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/app-shell";
import SettingsPage from "@/app/(workspace)/settings/page";
import OverviewPage from "@/app/(workspace)/overview/page";
import PnLPage from "@/app/(workspace)/pnl/page";
import { api } from "@/lib/api-client";
import type { ControlData, OverviewData } from "@/lib/types";

vi.mock("next/navigation", () => ({ usePathname: () => "/settings" }));

const control: ControlData = {
  as_of: "2026-10-27T12:00:00Z", owner: { full_name: "Owner", base_ccy: "EUR", tz: "UTC" },
  accounts: [{
    account_id: 1, config_key: "paper", display_name: "Local paper", broker_code: "paper", broker_name: "Local simulator",
    environment: "paper", base_ccy: "EUR", status: "connected", external_ref: null,
    paper_initial_equity: "100", paper_initial_cash: "100", credential: { present: false, status: null, expires_at: null },
  }],
  bindings: [{
    binding_id: 1, strategy_id: "trend", account_id: 1, mode: "off", asset_score_threshold: "0.51",
    max_position_pct: "0.0001", max_total_exposure_pct: "0.25", max_daily_loss_pct: "0.03",
    max_open_positions: 2, instruments_allowed: null, asset_classes_allowed: ["equity"],
  }],
  strategies: [], activity: [], modes: ["off", "close_only", "trading"], limits: {},
};
const overview: OverviewData = {
  as_of: control.as_of, owner: { display_name: "Owner", base_currency: "EUR", timezone: "UTC" },
  safety: { execution_mode: "paper", allow_live: false, environment: "dev" },
  accounts: [], strategies: null, freshness: { price_feeds: null, last_signal_at: null, last_fill_at: null }, activity: [],
};

describe("working workspace controls", () => {
  it.each(["overview", "pnl"])("shows the actual stored equity observation time on %s", async (route) => {
    const observed = "2026-09-05T16:59:41.833739Z";
    const account = {
      account_id: 1, name: "Stored account", currency: "EUR", starting_equity: null,
      equity: { value: "100", as_of: observed, source: "execution_snapshot" },
    };
    if (route === "overview") {
      vi.spyOn(api, "overview").mockResolvedValue({ ...overview, accounts: [{
        ...account, environment: "paper", status: "connected", equity_trend: [],
        realized_pnl: null, open_positions: 0, fills: 0,
      }] });
      render(<OverviewPage />);
    } else {
      vi.spyOn(api, "pnl").mockResolvedValue({ as_of: control.as_of, days: 90, accounts: [{
        ...account, equity_daily: [], equity_by_trade: [], realized_total: "0",
        realized: [], positions: [], fees: [],
      }] });
      render(<PnLPage />);
      await screen.findByText("Latest recorded equity");
    }
    const timestamp = await screen.findByText(observed);
    expect(timestamp.getAttribute("datetime")).toBe(observed);
  });

  it("treats a connected local simulator as healthy without requesting broker credentials", async () => {
    vi.spyOn(api, "control").mockResolvedValue(control);
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    const row = screen.getByRole("row", { name: /Local paper/ });
    expect(within(row).getByText("connected").className).toContain("positive-number");
    expect(row.textContent).toContain("Local simulator — no credential needed");
    expect(within(row).queryByText(/Missing/)).toBeNull();
  });

  it.each([
    ["revoked", null, "revoked"],
    ["active", "2000-01-01T00:00:00Z", "Expired"],
  ])("does not show a present %s broker credential as healthy", async (status, expires_at, expected) => {
    vi.spyOn(api, "control").mockResolvedValue({ ...control, accounts: [{
      ...control.accounts![0], broker_code: "coinbase", credential: { present: true, status, expires_at },
    }] });
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    const row = screen.getByRole("row", { name: /Local paper/ });
    expect(row.textContent).toContain(expected);
    expect(within(row).queryByText("✓ Present")).toBeNull();
  });

  it("links each settings navigation item to its real section", async () => {
    vi.spyOn(api, "control").mockResolvedValue(control);
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    const navigation = screen.getByRole("navigation", { name: "Settings sections" });
    for (const name of ["Owner Profile", "Broker Accounts", "Strategy Bindings", "Risk Limits"]) {
      const link = within(navigation).getByRole("link", { name });
      const href = link.getAttribute("href")!;
      expect(href.startsWith("#")).toBe(true);
      expect(document.getElementById(href.slice(1))).not.toBeNull();
    }
  });

  it("displays small risk fractions without rounding them to zero", async () => {
    vi.spyOn(api, "control").mockResolvedValue(control);
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.getByText("0.01%")).toBeTruthy();
    expect(screen.getByText("25.0%")).toBeTruthy();
    expect(screen.getByText("3.0%")).toBeTruthy();
  });

  it("offers working page search with Ctrl+K and Escape", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview);
    render(<AppShell><p>Workspace</p></AppShell>);
    await screen.findByText("API reachable");
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const search = await screen.findByRole("searchbox", { name: "Search pages" });
    fireEvent.change(search, { target: { value: "pnl" } });
    const results = screen.getByRole("navigation", { name: "Page search results" });
    expect(within(results).getByRole("link", { name: "Profit & Loss" }).getAttribute("href")).toBe("/pnl");
    expect(within(results).getAllByRole("link")).toHaveLength(1);
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("searchbox")).toBeNull());
    expect(screen.queryByRole("button", { name: "Notifications" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Theme" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Help" })).toBeNull();
  });
});
