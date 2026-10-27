"use client";

import { useEffect, useState } from "react";
import { Database, TrendingUp } from "lucide-react";
import { PageHeader, Panel } from "@/components/ui";
import { api, ApiError } from "@/lib/api-client";
import type { ControlData } from "@/lib/types";
import { formatAmount, formatPercent } from "@/lib/money";

function CredentialStatus({ account, asOf }: { account: NonNullable<ControlData["accounts"]>[number]; asOf: string }) {
  if (account.broker_code === "paper") return <span className="muted-number">Local simulator — no credential needed</span>;
  const credential = account.credential;
  if (!credential.present) return <span className="negative-number">Missing</span>;
  const expiry = credential.expires_at ? Date.parse(credential.expires_at) : null;
  const observedAt = Date.parse(asOf);
  const expiryUnknown = expiry !== null && (!Number.isFinite(expiry) || !Number.isFinite(observedAt));
  const expired = expiry !== null && !expiryUnknown && expiry <= observedAt;
  const active = credential.status === "active" && !expired && !expiryUnknown;
  return <span className={active ? "positive-number" : "negative-number"}>
    {expired ? "Expired" : expiryUnknown ? "Expiry unknown" : credential.status || "Status unknown"}
    {credential.expires_at && !expiryUnknown && <span className="table-subline">Expires {new Date(credential.expires_at).toLocaleString()}</span>}
  </span>;
}

export default function SettingsPage() {
  const [data, setData] = useState<ControlData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function loadData() {
      try {
        const control = await api.control();
        setData(control);
      } catch (err) {
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Failed to load settings");
        }
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  if (loading) {
    return <div className="loading-state">Loading settings...</div>;
  }

  if (error) {
    return (
      <div className="error-state">
        <p>{error}</p>
        <button onClick={() => window.location.reload()}>Retry</button>
      </div>
    );
  }

  if (!data) {
    return <div className="empty-state">No settings data available</div>;
  }

  return (
    <>
      <PageHeader
        eyebrow="WORKSPACE CONFIGURATION"
        title="Settings"
        description="Read your owner profile, broker accounts, binding modes, and recorded risk limits."
      />
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="Settings sections">
          <a className="settings-nav-item" href="#owner-profile">Owner Profile</a>
          <a className="settings-nav-item" href="#broker-accounts">Broker Accounts</a>
          <a className="settings-nav-item" href="#strategy-bindings">Strategy Bindings</a>
          <a className="settings-nav-item" href="#risk-limits">Risk Limits</a>
        </nav>
        <div className="settings-content">
          <Panel id="owner-profile" title="Owner profile" subtitle="Account owner information">
            {data.owner === null && <p role="status">Owner profile unavailable</p>}
            <div className="form-grid">
              <div className="form-field">
                <label>Full name</label>
                <input type="text" defaultValue={data.owner?.full_name || ""} disabled />
              </div>
              <div className="form-field">
                <label>Base currency</label>
                <input type="text" defaultValue={data.owner?.base_ccy || ""} disabled />
              </div>
              <div className="form-field">
                <label>Timezone</label>
                <input type="text" defaultValue={data.owner?.tz || ""} disabled />
              </div>
            </div>
            <div className="info-note" style={{ marginTop: "16px" }}>
              Owner profile changes require direct backend access. Contact the system administrator.
            </div>
          </Panel>

          <Panel id="broker-accounts" title="Broker accounts" subtitle={`Account and credential snapshot · ${new Date(data.as_of).toLocaleString()}`}>
            {data.accounts === null && <p role="status">Broker accounts unavailable</p>}
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>ACCOUNT</th>
                    <th>BROKER</th>
                    <th>ENVIRONMENT</th>
                    <th>CURRENCY</th>
                    <th>STATUS</th>
                    <th>CREDENTIALS</th>
                  </tr>
                </thead>
                <tbody>
                  {data.accounts?.map((account) => (
                    <tr key={account.account_id}>
                      <td>
                        <div className="table-primary">
                          <span className="table-symbol"><Database size={16} /></span>
                          <div>
                            <strong>{account.display_name}</strong>
                            <span>ID: {account.account_id}</span>
                          </div>
                        </div>
                      </td>
                      <td><strong>{account.broker_name}</strong></td>
                      <td><span className="version-code">{account.environment}</span></td>
                      <td>{account.base_ccy}</td>
                      <td>
                        <span className={account.status === "connected" ? "positive-number" : "negative-number"}>
                          {account.status}
                        </span>
                      </td>
                      <td>
                        <CredentialStatus account={account} asOf={data.as_of} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel id="strategy-bindings" title="Strategy bindings" subtitle="Recorded strategy-to-account execution modes">
            {data.bindings === null && <p role="status">Strategy bindings unavailable</p>}
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>BINDING ID</th>
                    <th>STRATEGY</th>
                    <th>ACCOUNT</th>
                    <th>MODE</th>
                  </tr>
                </thead>
                <tbody>
                  {data.bindings?.map((binding) => (
                    <tr key={binding.binding_id}>
                      <td><strong>#{binding.binding_id}</strong></td>
                      <td>{binding.strategy_id}</td>
                      <td>Account #{binding.account_id}</td>
                      <td><span className="version-code">{({ off: "Off", close_only: "Close only", trading: "Trading", custom: "Custom" } as Record<string, string>)[binding.mode] ?? binding.mode}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel id="risk-limits" title="Risk limits" subtitle="Recorded limits for each strategy binding; read only">
            {data.bindings === null && <p role="status">Risk limits unavailable</p>}
            {data.bindings?.length === 0 && <p>No strategy bindings recorded.</p>}
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>BINDING</th><th>SCORE THRESHOLD</th><th>MAX POSITION</th><th>MAX EXPOSURE</th><th>MAX DAILY LOSS</th><th>MAX OPEN POSITIONS</th></tr></thead>
                <tbody>{data.bindings?.map((binding) => <tr key={binding.binding_id}>
                  <td>#{binding.binding_id}</td>
                  <td>{formatAmount(binding.asset_score_threshold)}</td>
                  <td>{formatPercent(binding.max_position_pct)}</td>
                  <td>{formatPercent(binding.max_total_exposure_pct)}</td>
                  <td>{formatPercent(binding.max_daily_loss_pct)}</td>
                  <td>{binding.max_open_positions}</td>
                </tr>)}</tbody>
              </table>
            </div>
          </Panel>

          <Panel title="Available strategies" subtitle="Strategies available for binding">
            <div className="strategy-health-list">
              {data.strategies.map((strategy) => (
                <div className="strategy-health-item" key={strategy.strategy_id}>
                  <div className="strategy-symbol"><TrendingUp size={17} /></div>
                  <div className="strategy-health-copy">
                    <strong>{strategy.name}</strong>
                    <span>{strategy.strategy_id} · {strategy.asset_class}</span>
                  </div>
                  <div className="strategy-health-status">
                    <span className={strategy.released ? "positive-number" : "muted-number"}>
                      {strategy.released ? "Released" : "Draft"}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </>
  );
}
