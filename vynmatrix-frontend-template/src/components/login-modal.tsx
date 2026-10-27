"use client";

import { useState } from "react";
import { api, clearAdminKey, setAdminKey } from "@/lib/api-client";

export function LoginModal() {
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      await api.version(key);
      setAdminKey(key);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to authenticate");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-overlay">
      <div className="login-card">
        <div className="login-header">
          <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
            <path d="M5 8l7 16 4-9 4 9 7-16" />
          </svg>
          <h2>Unlock your dashboard</h2>
          <p>
            Paste the admin key of this installation. It is the value of
            <code>BACKEND_ADMIN_API_KEY</code> in your <code>.env</code> file.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="login-form">
          <label htmlFor="admin-key">Admin key</label>
          <input
            id="admin-key"
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            required
            spellCheck={false}
            autoComplete="off"
            placeholder="Enter your admin key"
          />

          {error && <div className="login-error" role="alert">{error}</div>}

          <button type="submit" className="button button-primary" disabled={loading}>
            {loading ? "Verifying..." : "Unlock"}
          </button>
        </form>

        <p className="login-note">
          The key stays in this browser tab only, is sent to this computer only, and is forgotten
          when you close the tab or press Lock.
        </p>
      </div>
    </div>
  );
}

export function LockButton() {
  function handleLock() {
    clearAdminKey();
  }

  return (
    <button className="icon-button" onClick={handleLock} title="Forget the admin key in this tab">
      Lock
    </button>
  );
}
