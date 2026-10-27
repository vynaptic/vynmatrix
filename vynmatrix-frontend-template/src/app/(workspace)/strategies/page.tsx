"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, Blocks, Search, TrendingUp } from "lucide-react";
import { PageHeader, Panel, StatusBadge } from "@/components/ui";
import { api, ApiError } from "@/lib/api-client";
import { formatMoneyTotals } from "@/lib/money";
import type { StrategiesData } from "@/lib/types";

export default function StrategiesPage() {
  const [data, setData] = useState<StrategiesData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    async function loadData() {
      try {
        const strategies = await api.strategies();
        setData(strategies);
      } catch (err) {
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Failed to load strategies");
        }
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  if (loading) {
    return <div className="loading-state">Loading strategies...</div>;
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
    return <div className="empty-state">No strategies available</div>;
  }

  const activeBindings = data.strategies.reduce((count, strategy) =>
    count + strategy.bindings.filter((binding) => binding.active).length, 0);
  const released = data.strategies.filter((strategy) => strategy.released).length;
  const realizedPnl = data.strategies.some((strategy) => strategy.realized_pnl === null)
    ? null
    : data.strategies.flatMap((strategy) => strategy.realized_pnl ?? []);
  const query = search.trim().toLowerCase();
  const visibleStrategies = data.strategies.filter((strategy) =>
    [strategy.name, strategy.strategy_id, strategy.asset_class].some((value) => value.toLowerCase().includes(query)));

  return (
    <>
      <PageHeader
        eyebrow="STRATEGY MANAGEMENT"
        title="Strategy Lab"
        description="View and manage your quantitative strategies and their bindings."
        actions={<Link className="button button-primary" href="/strategies/new">Create a strategy</Link>}
      />
      <div className="metrics-grid compact-metrics">
        <div className="metric-card">
          <div className="metric-top"><span>Total strategies</span><Blocks size={17} /></div>
          <div className="metric-value">{data.strategies.length.toString().padStart(2, "0")}</div>
          <div className="metric-change">Released: {released}</div>
        </div>
        <div className="metric-card">
          <div className="metric-top"><span>Active bindings</span><TrendingUp size={17} /></div>
          <div className="metric-value">{activeBindings.toString().padStart(2, "0")}</div>
          <div className="metric-change">Running on accounts</div>
        </div>
        <div className="metric-card">
          <div className="metric-top"><span>Total P&L</span><ArrowRight size={17} /></div>
          <div className="metric-value">
            {formatMoneyTotals(realizedPnl, true)}
          </div>
          <div className="metric-change">Realized across all strategies</div>
        </div>
        <div className="metric-card">
          <div className="metric-top"><span>Catalogue</span><ArrowRight size={17} /></div>
          <div className="metric-value">{data.strategies.length}</div>
          <div className="metric-change">Available strategies</div>
        </div>
      </div>
      <Panel
        title="Your strategies"
        subtitle={`As of ${new Date(data.as_of).toLocaleString()}`}
      >
        <div className="table-tools">
          <label className="table-search"><Search size={15} /><input aria-label="Search strategies" placeholder="Search strategies..." value={search} onChange={(event) => setSearch(event.target.value)} /></label>
          <div className="filter-chip">All strategies <span>{data.strategies.length}</span></div>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>STRATEGY</th>
                <th>ASSET CLASS</th>
                <th>VERSION</th>
                <th>STATUS</th>
                <th>BINDINGS</th>
                <th>REALIZED P&L</th>
              </tr>
            </thead>
            <tbody>
              {visibleStrategies.map((s) => (
                <tr key={s.strategy_id}>
                  <td>
                    <div className="table-primary">
                      <span className="table-symbol"><TrendingUp size={16} /></span>
                      <div>
                        <strong>{s.name}</strong>
                        <span>{s.strategy_id}</span>
                      </div>
                    </div>
                  </td>
                  <td><strong>{s.asset_class}</strong></td>
                  <td><span className="version-code">{s.version || "—"}</span></td>
                  <td>
                    <StatusBadge tone={s.released ? "success" : "neutral"}>
                      {s.released ? "Released" : "Draft"}
                    </StatusBadge>
                  </td>
                  <td>
                    <span className="muted-number">{s.bindings.length} binding{s.bindings.length !== 1 ? "s" : ""}</span>
                  </td>
                  <td>
                    <span>{formatMoneyTotals(s.realized_pnl, true)}</span>
                  </td>
                </tr>
              ))}
              {visibleStrategies.length === 0 && (
                <tr><td colSpan={6} className="empty-state">{query ? "No strategies match your search." : "No strategies available."}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}
