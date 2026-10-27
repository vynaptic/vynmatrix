"use client";

import { useEffect, useState } from "react";
import { Activity, Clock } from "lucide-react";
import { PageHeader, Panel } from "@/components/ui";
import { api, ApiError } from "@/lib/api-client";
import type { OverviewData } from "@/lib/types";

export default function ActivityPage() {
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
          setError("Failed to load activity data");
        }
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  if (loading) {
    return <div className="loading-state">Loading activity...</div>;
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
    return <div className="empty-state">No activity data available</div>;
  }

  const signalCount = data.activity.filter((a) => a.kind === "signal").length;
  const fillCount = data.activity.filter((a) => a.kind === "fill").length;

  return (
    <>
      <PageHeader
        eyebrow="AUDIT & FEEDBACK"
        title="Activity Timeline"
        description="Durable decisions and execution events from your strategies."
      />
      <div className="metrics-grid compact-metrics">
        <div className="metric-card">
          <div className="metric-top"><span>Total events</span><Activity size={17} /></div>
          <div className="metric-value">{data.activity.length.toString()}</div>
          <div className="metric-change">Signals and fills</div>
        </div>
        <div className="metric-card">
          <div className="metric-top"><span>Signals</span><Clock size={17} /></div>
          <div className="metric-value">{signalCount.toString()}</div>
          <div className="metric-change">Strategy decisions</div>
        </div>
        <div className="metric-card">
          <div className="metric-top"><span>Fills</span><Activity size={17} /></div>
          <div className="metric-value">{fillCount.toString()}</div>
          <div className="metric-change">Executed orders</div>
        </div>
      </div>
      <Panel
        title="Recent activity"
        subtitle={`As of ${new Date(data.as_of).toLocaleString()}`}
      >
        <div className="activity-list">
          {data.activity.map((event) => (
            <div className="activity-item" key={`${event.kind}-${event.at}`}>
              <div className={`activity-rail-icon ${event.kind === "signal" ? "info" : "success"}`}>
                <Activity size={14} />
              </div>
              <div className="activity-copy">
                <strong>
                  {event.kind === "signal" ? "Signal recorded" : "Fill recorded"}
                </strong>
                <p>
                  {event.kind === "signal"
                    ? `${event.strategy_id} · ${event.action} · ${event.symbol}${event.confidence ? ` · confidence: ${event.confidence}` : ""}`
                    : `${event.account_id} · ${event.side} ${event.quantity} ${event.symbol} @ ${event.price}${event.fee ? ` · fee: ${event.fee} ${event.fee_currency}` : ""}`}
                </p>
              </div>
              <time>{new Date(event.at).toLocaleString()}</time>
            </div>
          ))}
        </div>
        {data.activity.length === 0 && (
          <div className="empty-state" style={{ padding: "40px" }}>
            No activity recorded yet.
          </div>
        )}
      </Panel>
    </>
  );
}
