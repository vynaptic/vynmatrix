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
  const [request, setRequest] = useState({ days: 90 });
  const { days } = request;

  useEffect(() => {
    let active = true;
    async function loadData() {
      try {
        const pnl = await api.pnl(request.days);
        if (active) setData(pnl);
      } catch (err) {
        if (active) setError(err instanceof ApiError ? err.message : "Failed to load P&L data");
      } finally {
        if (active) setLoading(false);
      }
    }
    loadData();
    return () => { active = false; };
  }, [request]);

  function selectPeriod(nextDays: number) {
    setData(null);
    setError(null);
    setLoading(true);
    setRequest({ days: nextDays });
  }

  return (
    <>
      <PageHeader
        eyebrow="PERFORMANCE ANALYTICS"
        title="Profit & Loss"
        description="Daily equity history, cumulative realized P&L, and current positions across your accounts."
        actions={
          <div className="header-actions">
            <button
              className={`button ${days === 30 ? "button-primary" : "button-secondary"}`}
              aria-pressed={days === 30}
              onClick={() => selectPeriod(30)}
            >
              30 days
            </button>
            <button
              className={`button ${days === 90 ? "button-primary" : "button-secondary"}`}
              aria-pressed={days === 90}
              onClick={() => selectPeriod(90)}
            >
              90 days
            </button>
            <button
              className={`button ${days === 180 ? "button-primary" : "button-secondary"}`}
              aria-pressed={days === 180}
              onClick={() => selectPeriod(180)}
            >
              180 days
            </button>
          </div>
        }
      />
      {loading && <div className="loading-state" role="status">Loading P&L data...</div>}
      {error && (
        <div className="error-state" role="alert">
          <p>{error}</p>
          <button className="button button-secondary" onClick={() => selectPeriod(days)}>Retry</button>
        </div>
      )}
      {!loading && !error && (!data || data.accounts.length === 0) && (
        <div className="empty-state">No P&L data available</div>
      )}
      {data && <div className="dashboard-grid">
        {data.accounts.map((account) => (
          <Panel
            key={account.account_id}
            title={account.name}
            subtitle={`${account.currency} · Recorded account snapshot`}
            className="performance-panel"
          >
            <div className="performance-summary">
              <div>
                <strong>
                  {formatMoney(account.realized_total, account.currency, true)}
                </strong>
                <span>Cumulative realized P&L</span>
              </div>
              <div>
                <strong>{formatMoney(account.equity?.value, account.currency)}</strong>
                <span>Latest recorded equity</span>
                {account.equity?.as_of && <span className="recorded-at">Recorded at <time dateTime={account.equity.as_of}>{account.equity.as_of}</time></span>}
              </div>
            </div>

            <div style={{ marginTop: "20px" }}>
              <h3 style={{ fontSize: "12px", color: "var(--muted)", marginBottom: "12px" }}>Daily equity · Last {days} days</h3>
              {account.equity_daily === null ? (
                <p>Daily equity history unavailable</p>
              ) : account.equity_daily.length === 0 ? (
                <p>No daily equity history in the last {days} days</p>
              ) : (
                <div className="table-scroll">
                  <table className="data-table" aria-label={`${account.name} daily equity · Last ${days} days`}>
                    <thead>
                      <tr><th scope="col">DATE</th><th scope="col">EQUITY</th></tr>
                    </thead>
                    <tbody>
                      {account.equity_daily.map((point) => (
                        <tr key={point.date}>
                          <td>{point.date}</td>
                          <td>{formatMoney(point.value, account.currency)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <div style={{ marginTop: "20px" }}>
              <h3 style={{ fontSize: "12px", color: "var(--muted)", marginBottom: "12px" }}>Current open positions</h3>
              {account.positions === null ? (
                <p>Positions unavailable</p>
              ) : account.positions.length === 0 ? (
                <p>No open positions</p>
              ) : (
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
              )}
            </div>

            {account.realized && account.realized.length > 0 && (
              <div style={{ marginTop: "20px" }}>
                <h3 style={{ fontSize: "12px", color: "var(--muted)", marginBottom: "12px" }}>Cumulative realized P&L by strategy</h3>
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
      </div>}
    </>
  );
}
