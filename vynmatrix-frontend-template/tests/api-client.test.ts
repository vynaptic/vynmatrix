import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, clearAdminKey, isAuthenticated, setAdminKey } from "@/lib/api-client";

beforeEach(() => clearAdminKey());
afterEach(() => clearAdminKey());

describe("admin-key API boundary", () => {
  it("normalizes headers and sends the tab key on relative UI requests", async () => {
    const request = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", request);
    setAdminKey("test-key");
    await api.overview();
    expect(request.mock.calls[0][0]).toBe("/api/ui/overview");
    expect(request.mock.calls[0][1].headers.get("X-Admin-Key")).toBe("test-key");
  });

  it("clears authentication when the backend rejects a stored key", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("rejected", { status: 401 })));
    setAdminKey("rejected-key");
    await expect(api.overview()).rejects.toMatchObject({ status: 401 });
    expect(isAuthenticated()).toBe(false);
  });

  it("does not let a stale unauthorized response clear a newer tab credential", async () => {
    let complete!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise<Response>((resolve) => { complete = resolve; })));
    setAdminKey("old-key");
    const pending = api.overview();
    clearAdminKey();
    setAdminKey("new-key");
    complete(new Response("old key rejected", { status: 401 }));
    await expect(pending).rejects.toMatchObject({ status: 401 });
    expect(isAuthenticated()).toBe(true);
    expect(window.sessionStorage.getItem("adminKey")).toBe("new-key");
  });

  it("keeps authentication tab-local when browser storage is disabled", () => {
    for (const method of ["getItem", "setItem", "removeItem"] as const) {
      vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new DOMException("Storage disabled", "SecurityError"); });
    }
    setAdminKey("memory-only-test-key");
    expect(isAuthenticated()).toBe(true);
    clearAdminKey();
    expect(isAuthenticated()).toBe(false);
  });

  it("validates the entered login key instead of overwriting it with an older key", async () => {
    const request = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", request);
    setAdminKey("previous-key");
    await api.version("entered-key");
    expect(request.mock.calls[0][0]).toBe("/api/ui/version");
    expect(request.mock.calls[0][1].headers.get("X-Admin-Key")).toBe("entered-key");
  });

  it("shows the backend error detail without exposing raw JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "invalid or missing admin key" }), { status: 401 })));
    await expect(api.version("invalid-test-key")).rejects.toMatchObject({
      status: 401, message: "invalid or missing admin key",
    });
  });

  it.each([
    ["History service unavailable", "History service unavailable"],
    ["<html>Proxy failure</html>", "Request failed (503)"],
  ])("handles a non-JSON error body safely", async (body, message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 503 })));
    await expect(api.pnl()).rejects.toMatchObject({ status: 503, message });
  });
});
