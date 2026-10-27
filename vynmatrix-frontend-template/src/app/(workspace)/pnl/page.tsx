"use client";

import { useEffect, useState } from "react";
import { TrendingUp } from "lucide-react";
import { PageHeader, Panel } from "@/components/ui";
import { api, ApiError } from "@/lib/api-client";
import { formatAmount, formatMoney } from "@/lib/money";
import type { PnLData } from "@/lib/types";

export default function PnLPage() {
  const [data, setData] = useState<PnLData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(90);

  useEffect(() => {
    async function loadData() {
      try {
        const pnl = await api.pnl(days);
        setData(pnl);
      } catch (err) {
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Failed to load P&L data");
        }
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [days]);

  if (loading) {
    return <div className="loading-state">Loading P&L data...</div>;
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
    return <div className="empty-state">No P&L data available</div>;
  }

  return (
    <>
      <PageHeader
        eyebrow="PERFORMANCE ANALYTICS"
        title="Profit & Loss"
        description="Equity curves, realized P&L, and open positions across your accounts."
        actions={
          <div className="header-actions">
            <button
              className={`button ${days === 30 ? "button-primary" : "button-secondary"}`}
              onClick={() => setDays(30)}
            >
              30 days
            </button>
            <button
              className={`button ${days === 90 ? "button-primary" : "button-secondary"}`}
              onClick={() => setDays(90)}
            >
              90 days
            </button>
            <button
              className={`button ${days === 180 ? "button-primary" : "button-secondary"}`}
              onClick={() => setDays(180)}
            >
              180 days
            </button>
          </div>
        }
      />
      <div className="dashboard-grid">
        {data.accounts.map((account) => (
          <Panel
            key={account.account_id}
            title={account.name}
            subtitle={`${account.currency} · Last ${days} days`}
            className="performance-panel"
          >
            <div className="performance-summary">
              <div>
                <strong>
                  {formatMoney(account.realized_total, account.currency, true)}
                </strong>
                <span>Realized P&L</span>
              </div>
              <span>
                {formatMoney(account.equity?.value, account.currency)}
              </span>
            </div>

            {account.positions && account.positions.length > 0 && (
              <div style={{ marginTop: "20px" }}>
                <h3 style={{ fontSize: "12px", color: "var(--muted)", marginBottom: "12px" }}>Open Positions</h3>
                <div className="table-scroll">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>SYMBOL</th>
                        <th>QUANTITY</th>
                        <th>AVG PRICE</th>
                        <th>LAST MARK</th>
                        <th>NOTIONAL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {account.positions.map((pos, idx) => (
                        <tr key={idx}>
                          <td><strong>{pos.symbol}</strong></td>
                          <td>{pos.quantity}</td>
                          <td>{formatAmount(pos.average_price)}{pos.average_price !== null && " · Quote currency unavailable"}</td>
                          <td>{formatAmount(pos.last_mark)}{pos.last_mark !== null && " · Quote currency unavailable"}</td>
                          <td>{formatMoney(pos.notional, pos.notional_currency)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {account.realized && account.realized.length > 0 && (
              <div style={{ marginTop: "20px" }}>
                <h3 style={{ fontSize: "12px", color: "var(--muted)", marginBottom: "12px" }}>Realized P&L by Strategy</h3>
                <div className="strategy-health-list">
                  {account.realized.map((item, idx) => (
                    <div className="strategy-health-item" key={idx}>
                      <div className="strategy-symbol"><TrendingUp size={17} /></div>
                      <div className="strategy-health-copy">
                        <strong>{item.strategy_id}</strong>
                        <span>{item.symbol}</span>
                      </div>
                      <div className="strategy-health-status">
                        <span className={item.value?.startsWith("-") ? "negative-number" : "positive-number"}>
                          {formatMoney(item.value, account.currency, true)}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Panel>
        ))}
      </div>
    </>
  );
}
