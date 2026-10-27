import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "@/components/auth-provider";
import { AppShell } from "@/components/app-shell";
import SettingsPage from "@/app/(workspace)/settings/page";
import { api, clearAdminKey, setAdminKey } from "@/lib/api-client";
import type { ControlData, OverviewData } from "@/lib/types";

vi.mock("next/navigation", () => ({ usePathname: () => "/overview" }));

const overview: OverviewData = {
  as_of: "2026-10-27T12:00:00Z",
  safety: { execution_mode: "paper", allow_live: false, environment: "dev" },
  owner: { display_name: "Owner", base_currency: "EUR", timezone: "UTC" },
  accounts: [], strategies: { catalogue: 0, released: 0, bound: 0, active: 0 },
  freshness: { price_feeds: null, last_signal_at: null, last_fill_at: null }, activity: [],
};

beforeEach(() => clearAdminKey());

describe("authentication and operational status", () => {
  it("locks a mounted authenticated view when its key is cleared", async () => {
    setAdminKey("test-key");
    render(<AuthProvider><p>Private workspace</p></AuthProvider>);
    await screen.findByText("Private workspace");
    act(() => clearAdminKey());
    await screen.findByRole("heading", { name: "Unlock your dashboard" });
    expect(screen.queryByText("Private workspace")).toBeNull();
  });

  it("hydrates an existing tab key without a markup mismatch", async () => {
    setAdminKey("test-key");
    const view = <AuthProvider><p>Private workspace</p></AuthProvider>;
    const container = document.createElement("div");
    container.innerHTML = renderToString(view);
    document.body.append(container);
    const recoverableError = vi.fn();
    render(view, { container, hydrate: true, onRecoverableError: recoverableError });
    await screen.findByText("Private workspace");
    expect(recoverableError).not.toHaveBeenCalled();
  });

  it("unlocks through the configured API and forgets rejected credentials", async () => {
    const request = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", request);
    render(<AuthProvider><p>Private workspace</p></AuthProvider>);
    fireEvent.change(await screen.findByLabelText("Admin key"), { target: { value: "test-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    await screen.findByText("Private workspace");
    expect(request).toHaveBeenCalledWith("/api/ui/version", expect.any(Object));
    request.mockResolvedValue(new Response("unauthorized", { status: 401 }));
    await act(async () => { await expect(api.overview()).rejects.toMatchObject({ status: 401 }); });
    await screen.findByRole("heading", { name: "Unlock your dashboard" });
    expect(window.sessionStorage.getItem("adminKey")).toBeNull();
  });

  it("reports API connectivity without certifying database or worker health", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview);
    render(<AppShell><p>Workspace</p></AppShell>);
    await screen.findByText("API reachable");
    expect(screen.queryByText("All systems operational")).toBeNull();
    expect(screen.getByText(/Worker health is not checked/)).toBeTruthy();
  });

  it("shows unavailable API status and unknown safety on connection failure", async () => {
    vi.spyOn(api, "overview").mockRejectedValue(new Error("offline"));
    render(<AppShell><p>Workspace</p></AppShell>);
    await screen.findByText("API unavailable");
    expect(screen.getByText("UNKNOWN")).toBeTruthy();
  });

  it("shows close-only execution authority without labeling it inactive", async () => {
    const binding = {
      binding_id: 1, strategy_id: "trend", account_id: 1, mode: "close_only",
      asset_score_threshold: "0.5", max_position_pct: "0.1", max_total_exposure_pct: "1",
      max_daily_loss_pct: "0.1", max_open_positions: 2, instruments_allowed: null,
      asset_classes_allowed: ["equity"],
    };
    const control: ControlData = {
      as_of: overview.as_of, owner: { full_name: "Owner", base_ccy: "EUR", tz: "UTC" },
      accounts: [], bindings: [binding], strategies: [], activity: [],
      modes: ["off", "close_only", "trading"], limits: {},
    };
    vi.spyOn(api, "control").mockResolvedValue(control);
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    const row = screen.getByRole("row", { name: /trend/ });
    expect(within(row).queryByText("Inactive")).toBeNull();
    expect(row.textContent).toContain("Close only");
    await waitFor(() => expect(screen.getByDisplayValue("Owner")).toBeTruthy());
  });

  it("shows unavailable settings sections without crashing or inventing empty state", async () => {
    vi.spyOn(api, "control").mockResolvedValue({
      as_of: overview.as_of, owner: null, accounts: null, bindings: null,
      strategies: [], activity: null, modes: [], limits: {},
    });
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.getByText("Owner profile unavailable")).toBeTruthy();
    expect(screen.getByText("Broker accounts unavailable")).toBeTruthy();
    expect(screen.getByText("Strategy bindings unavailable")).toBeTruthy();
  });
});
