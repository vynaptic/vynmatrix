import { formatMoney } from "@/lib/money";
import type { Json, JsonObject, JobKind } from "@/lib/research-types";

function object(value: Json | undefined): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : null;
}
function scalar(value: Json | undefined): string {
  return value === undefined ? "Unavailable" : value === null ? "Undefined" : typeof value === "object" ? "See artifact" : String(value);
}
export function JobResults({ kind, result }: { kind: JobKind; result: JsonObject }) {
  const metrics = object(result.metrics);
  const holdout = object(object(result.validation)?.holdout_reuse);
  const predictions = Array.isArray(result.predictions) ? result.predictions.map(object).filter((row): row is JsonObject => row !== null) : [];
  return <>
    {holdout?.holdout_reused === true && <div className="research-notice" role="status"><strong>Holdout reused.</strong> These metrics are not an independent evaluation. The recorded validation evidence shows the matching prior training attempts or original test observations.</div>}
    {kind === "backtest" && <BacktestResults result={result} />}
    {(kind === "train" || kind === "predict") && metrics && <>
      <h3>Forecast metrics</h3><p>Returns are unitless. Directional accuracy is a fraction. Undefined metrics remain undefined.</p>
      <div className="table-scroll"><table className="data-table"><thead><tr><th>FORECAST</th><th>SAMPLES</th><th>MAE</th><th>RMSE</th><th>DIRECTIONAL ACCURACY</th></tr></thead><tbody>
        {Object.entries({ model: "Fitted model", zero_return: "Zero-return baseline", training_mean: "Training-mean baseline" }).map(([key, label]) => {
          const row = object(metrics[key]);
          return <tr key={key}><td>{label}</td><td>{scalar(row?.count)}</td><td>{scalar(row?.mae)}</td><td>{scalar(row?.rmse)}</td><td>{scalar(row?.directional_accuracy)}</td></tr>;
        })}
      </tbody></table></div>
    </>}
    {predictions.length > 0 && <><h3>Recorded predictions</h3><p>Showing {Math.min(predictions.length, 100)} of {predictions.length}. Export the job result for every output and its frozen inputs.</p><div className="table-scroll"><table className="data-table"><thead><tr><th>RECORDED TIME</th><th>PREDICTED LOG RETURN</th><th>ACTUAL LOG RETURN</th></tr></thead><tbody>{predictions.slice(0, 100).map((row, index) => <tr key={index}><td><time dateTime={String(row.ts)}>{scalar(row.ts)}</time></td><td>{scalar(row.predicted_log_return)}</td><td>{scalar(row.actual_log_return)}</td></tr>)}</tbody></table></div></>}
    {result.validation !== undefined && <details open><summary>Validation, split and holdout evidence</summary><pre className="research-json">{JSON.stringify(result.validation, null, 2)}</pre></details>}
    <details><summary>Complete recorded result artifact</summary><pre className="research-json" aria-label="Job result">{JSON.stringify(result, null, 2)}</pre></details>
  </>;
}

function money(value: Json | undefined, currency: string | undefined) {
  const supplied = typeof value === "string" || typeof value === "number" ? String(value) : null;
  return formatMoney(supplied, currency);
}
function BacktestResults({ result }: { result: JsonObject }) {
  const currency = typeof result.currency === "string" ? result.currency : undefined;
  const trades = Array.isArray(result.trades) ? result.trades.map(object).filter((row): row is JsonObject => row !== null) : [];
  const curve = Array.isArray(result.equity_curve) ? result.equity_curve.map(object).filter((row): row is JsonObject => row !== null) : [];
  return <>
    <h3>Trusted historical result</h3>
    <div className="research-notice"><strong>{result.selection_eligible === true ? "Eligible for research selection" : result.selection_eligible === false ? "Ineligible for selection" : "Selection eligibility unavailable"}</strong>{Array.isArray(result.selection_blockers) && result.selection_blockers.length > 0 && <ul>{result.selection_blockers.map((blocker, index) => <li key={index}>{String(blocker)}</li>)}</ul>}</div>
    <dl className="research-metrics"><div><dt>Research initial capital</dt><dd>{money(result.initial_capital, currency)}</dd></div><div><dt>Research final equity</dt><dd>{money(result.final_equity, currency)}</dd></div><div><dt>Closed trades</dt><dd>{scalar(result.total_trades)}</dd></div><div><dt>Sharpe ratio</dt><dd>{scalar(result.sharpe_ratio)}</dd></div><div><dt>Total return (%)</dt><dd>{scalar(result.total_return_pct)}</dd></div><div><dt>Maximum drawdown (%)</dt><dd>{scalar(result.max_drawdown_pct)}</dd></div></dl>
    {result.terminal_position && <><h3>Unliquidated terminal position</h3><pre className="research-json">{JSON.stringify(result.terminal_position, null, 2)}</pre></>}
    <details open><summary>Research cost breakdown</summary><p>Simulated costs use the frozen research assumptions. These values are separate from recorded paper account balances and fees.</p><pre className="research-json">{JSON.stringify(result.cost_breakdown ?? null, null, 2)}</pre></details>
    <h3>Trusted closed trades</h3>
    {!Array.isArray(result.trades) ? <p>Closed trade detail unavailable.</p> : !trades.length ? <p>No closed trades recorded. Terminal exposure and selection blockers remain authoritative.</p> : <><p>Showing {Math.min(trades.length, 100)} of {trades.length} trades.</p><div className="table-scroll"><table className="data-table"><thead><tr><th>INSTRUMENT / SIDE</th><th>ENTRY</th><th>EXIT</th><th>QUANTITY</th><th>ENTRY PRICE</th><th>EXIT PRICE</th><th>FEES</th><th>NET P&amp;L</th></tr></thead><tbody>{trades.slice(0, 100).map((trade, index) => <tr key={index}><td>{scalar(trade.symbol)} · {scalar(trade.side)}</td><td><time dateTime={String(trade.entry_ts)}>{scalar(trade.entry_ts)}</time></td><td><time dateTime={String(trade.exit_ts)}>{scalar(trade.exit_ts)}</time></td><td>{scalar(trade.quantity)}</td><td>{money(trade.entry_price, currency)}</td><td>{money(trade.exit_price, currency)}</td><td>{money(trade.fees, currency)}</td><td>{money(trade.pnl, currency)}</td></tr>)}</tbody></table></div></>}
    {curve.length > 0 && <details><summary>Trusted equity observations ({curve.length})</summary><p>Preview of the first {Math.min(curve.length, 100)} observations; export contains the complete curve.</p><div className="table-scroll"><table className="data-table"><thead><tr><th>RECORDED TIME</th><th>RESEARCH EQUITY</th></tr></thead><tbody>{curve.slice(0, 100).map((point, index) => <tr key={index}><td><time dateTime={String(point.at)}>{scalar(point.at)}</time></td><td>{money(point.value, currency)}</td></tr>)}</tbody></table></div></details>}
  </>;
}
