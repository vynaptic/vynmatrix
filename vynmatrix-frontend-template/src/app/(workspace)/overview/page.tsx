"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Activity, ArrowRight, Blocks, ShieldCheck, TrendingUp } from "lucide-react";
import { MetricCard, PageHeader, Panel, StatusBadge } from "@/components/ui";
import { api, ApiError } from "@/lib/api-client";
import { formatMoney, formatMoneyTotals } from "@/lib/money";
import type { OverviewData } from "@/lib/types";

export default function OverviewPage() {
  const [data, setData] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function loadData() {
      try {
        const overview = await api.overview();
        setData(overview);
      } catch (err) {
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Failed to load overview data");
        }
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  if (loading) {
    return <div className="loading-state">Loading overview...</div>;
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
    return <div className="empty-state">No data available</div>;
  }

  const isPaper = data.safety.execution_mode === "paper" && !data.safety.allow_live;
  const greeting = new Date().getHours() < 12 ? "Good morning" : new Date().getHours() < 18 ? "Good afternoon" : "Good evening";
  const today = new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" }).toUpperCase();

  const equity = formatMoneyTotals(data.accounts.map((account) => ({
    currency: account.currency, value: account.equity?.value,
  })));
  const totalFills = data.accounts.some((account) => account.fills === null)
    ? "Unavailable"
    : data.accounts.reduce((sum, account) => sum + (account.fills ?? 0), 0).toString();
  const totalPositions = data.accounts.some((account) => account.open_positions === null)
    ? "Unavailable"
    : data.accounts.reduce((sum, account) => sum + (account.open_positions ?? 0), 0).toString();

  return (
    <>
      <PageHeader
        eyebrow={today}
        title={`${greeting}, ${data.owner.display_name}`}
        description="Here's what's happening across your quant workspace."
      />
      {isPaper && (
        <div className="notice-banner">
          <div className="notice-icon"><ShieldCheck size={18} /></div>
          <div><strong>Paper environment is active</strong><p>Live orders are disabled. All executions are simulated.</p></div>
          <span className="notice-pill">SAFE MODE</span>
        </div>
      )}
      <div className="metrics-grid">
        <MetricCard
          label="Total equity"
          value={equity}
          change={`${data.accounts.length} account${data.accounts.length !== 1 ? "s" : ""}`}
          tone="blue"
          icon={<TrendingUp size={18} />}
        />
        <MetricCard
          label="Active strategies"
          value={data.strategies?.active.toString() ?? "Unavailable"}
          change={data.strategies ? `${data.strategies.bound} bound to accounts` : "Bindings unavailable"}
          tone="green"
          icon={<Blocks size={18} />}
        />
        <MetricCard
          label="Open positions"
          value={totalPositions}
          change={`Across ${data.accounts.length} accounts`}
          tone="purple"
          icon={<Activity size={18} />}
        />
        <MetricCard
          label="Total fills"
          value={totalFills}
          change="Recorded executions"
          tone="orange"
          icon={<ShieldCheck size={18} />}
        />
      </div>
      <div className="dashboard-grid">
        <Panel
          title="Accounts overview"
          subtitle="Account equity and positions"
          actions={<Link href="/pnl" className="text-button">View P&L <ArrowRight size={14} /></Link>}
        >
          <div className="strategy-health-list">
            {data.accounts.map((account) => (
              <div className="strategy-health-item" key={account.account_id}>
                <div className="strategy-symbol"><Activity size={17} /></div>
                <div className="strategy-health-copy">
                  <strong>{account.name}</strong>
                  <span>{account.environment} · {account.currency}</span>
                </div>
                <div className="strategy-health-status">
                  <StatusBadge tone={account.status === "active" ? "success" : "warning"}>{account.status}</StatusBadge>
                  <span>{formatMoney(account.equity?.value, account.currency)}</span>
                </div>
              </div>
            ))}
          </div>
        </Panel>
        <Panel
          title="Recent activity"
          subtitle="Signals and fills"
          actions={<Link href="/activity" className="text-button">View audit log <ArrowRight size={14} /></Link>}
        >
          <div className="activity-list">
            {data.activity.slice(0, 5).map((event) => (
              <div className="activity-item" key={`${event.kind}-${event.at}`}>
                <div className={`activity-rail-icon ${event.kind === "signal" ? "info" : "success"}`}>
                  <Activity size={14} />
                </div>
                <div className="activity-copy">
                  <strong>{event.kind === "signal" ? "Signal recorded" : "Fill recorded"}</strong>
                  <p>
                    {event.kind === "signal"
                      ? `${event.strategy_id} · ${event.action} · ${event.symbol}`
                      : `${event.account_id} · ${event.side} ${event.quantity} ${event.symbol} @ ${event.price}`}
                  </p>
                </div>
                <time>{new Date(event.at).toLocaleTimeString()}</time>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </>
  );
}
