"use client";

import { useState, type FormEvent } from "react";
import { PageHeader, Panel } from "@/components/ui";
import { researchApi } from "@/lib/research-api";
import { formatAmount, formatMoney } from "@/lib/money";
import type { JsonObject, PaperAccountInput } from "@/lib/research-types";
import { errorMessage, Feedback, Field, isConflict, JsonView, RecordedTime, useResource } from "@/components/research/common";
import { ConfirmationDialog } from "@/components/research/confirmation-dialog";
import { modeConsequence, PaperBinding } from "@/components/research/paper-binding";

type Confirmation = { description: string; lines: string[]; consequence: string; operation: () => Promise<unknown>; success: string };
const accountFields: PaperAccountInput = { config_key: "", display_name: "", base_ccy: "", paper_initial_equity: "", paper_initial_cash: "" };
export default function PaperTradingPage() {
  const resource = useResource(researchApi.paper);
  const [account, setAccount] = useState(accountFields);
  const [strategyId, setStrategyId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [mode, setMode] = useState("off");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [requiresReview, setRequiresReview] = useState(false);
  const data = resource.data;
  const safe = Boolean(data && !resource.error && data.safety.execution_mode === "paper" && data.safety.allow_live === false);
  const locked = busy || resource.loading || !safe || requiresReview;
  const accounts = data?.control.accounts?.filter((item) => item.broker_code === "paper" && item.environment === "paper") ?? [];
  async function confirm() {
    if (!confirmation || locked) return;
    setBusy(true); setError(null); setMessage(null);
    try { await confirmation.operation(); setMessage(confirmation.success); setConfirmation(null); resource.refresh(); }
    catch (failure) {
      setConfirmation(null);
      if (isConflict(failure)) { setRequiresReview(true); setError(`${errorMessage(failure)}. Review the refreshed values and confirm a new change.`); resource.refresh(); }
      else setError(errorMessage(failure));
    } finally { setBusy(false); }
  }
  function reviewAccount(event: FormEvent) {
    event.preventDefault();
    const payload = { ...account };
    setConfirmation({ description: `Create local paper account ${payload.display_name}`, lines: [`Account key: ${payload.config_key}`, `Initial equity: ${payload.paper_initial_equity} ${payload.base_ccy}`, `Initial cash: ${payload.paper_initial_cash} ${payload.base_ccy}`], consequence: "Creates a local paper account with this recorded starting capital. It does not connect a live broker or place orders.", operation: () => researchApi.createPaperAccount(payload), success: "Paper account created." });
  }
  function reviewBinding(event: FormEvent) {
    event.preventDefault(); const payload = { strategy_id: strategyId, broker_account_id: Number(accountId), mode };
    setConfirmation({ description: "Create a paper strategy binding", lines: [`Strategy: ${strategyId}`, `Paper account: ${accounts.find((item) => item.account_id === Number(accountId))?.display_name ?? accountId}`, `Unbound → ${mode}`], consequence: `The binding follows the strategy's active released version. ${modeConsequence[mode]}`, operation: () => researchApi.bindPaper(payload), success: "Paper binding created." });
  }
  function reviewUpdate(id: number, expected: JsonObject, changes: JsonObject) {
    setConfirmation({ description: `Update paper binding ${id}`, lines: Object.keys(changes).map((key) => `${key}: ${expected[key]} → ${changes[key]}`), consequence: `${modeConsequence[String(changes.mode)] ?? "Risk changes apply to subsequent paper decisions and remain subject to server safety gates."} The binding follows its active released version.`, operation: () => researchApi.updatePaperBinding(id, { expected, changes }), success: `Paper binding ${id} updated.` });
  }
  return <>
    <PageHeader eyebrow="RECORDED PAPER EXECUTION" title="Paper Trading" description="Inspect local paper accounts and confirm strategy authority and risk changes." actions={<button className="button button-secondary" disabled={busy} onClick={resource.refresh}>Refresh paper state</button>} />
    <Feedback error={resource.error || error} message={message} />
    {resource.loading && <p role="status">Loading paper accounts…</p>}
    {data && <>
      <div className="research-notice"><strong>{safe ? "Paper execution confirmed" : "Paper controls are locked: runtime safety could not be established."}</strong><span>Observed <RecordedTime value={data.as_of} /> · Mode {data.safety.execution_mode} · Allow live {String(data.safety.allow_live)}</span></div>
      {requiresReview && <div className="research-notice"><p>Review the refreshed values before preparing another change.</p><button className="button button-secondary" disabled={Boolean(resource.error) || resource.loading || busy} onClick={() => { setRequiresReview(false); setError(null); }}>I reviewed the refreshed values</button></div>}
      <Panel title="Local paper accounts" subtitle="Only local paper broker accounts are eligible for these controls.">
        {data.control.accounts === null ? <p role="status">Paper accounts unavailable.</p> : !accounts.length ? <p>No local paper accounts. Create an account with explicit starting capital.</p> : <div className="table-scroll"><table className="data-table"><thead><tr><th>ACCOUNT</th><th>STATUS</th><th>INITIAL EQUITY</th><th>INITIAL CASH</th></tr></thead><tbody>{accounts.map((item) => <tr key={item.account_id}><td>{item.display_name}<span className="table-subline">{item.config_key} · {item.account_id}</span></td><td>{item.status}</td><td>{formatMoney(item.paper_initial_equity, item.base_ccy)}</td><td>{formatMoney(item.paper_initial_cash, item.base_ccy)}</td></tr>)}</tbody></table></div>}
      </Panel>
      <Panel title="Create a local paper account" subtitle="Capital remains in the explicit account currency.">
        <form onSubmit={reviewAccount}><div className="form-grid">{Object.entries(account).map(([key, value]) => <Field label={{ config_key: "Account key", display_name: "Account name", base_ccy: "Account currency", paper_initial_equity: "Initial equity", paper_initial_cash: "Initial cash" }[key]!} key={key}><input required value={value} inputMode={key.startsWith("paper_initial") ? "decimal" : "text"} onChange={(event) => setAccount((old) => ({ ...old, [key]: key === "base_ccy" ? event.target.value.toUpperCase() : event.target.value }))} /></Field>)}</div><button className="button button-primary" disabled={locked || Object.values(account).some((value) => !value.trim())}>Review account</button></form>
      </Panel>
      <Panel title="Bind a released strategy" subtitle="A binding follows the active released version; runtime selection and server gates still apply.">
        <form onSubmit={reviewBinding}><div className="form-grid">
          <Field label="Released strategy"><select required value={strategyId} onChange={(event) => setStrategyId(event.target.value)}><option value="">Choose a released strategy</option>{data.control.strategies.filter((strategy) => strategy.released).map((strategy) => <option value={strategy.strategy_id} key={strategy.strategy_id}>{strategy.name}</option>)}</select></Field>
          <Field label="Binding paper account"><select required value={accountId} onChange={(event) => setAccountId(event.target.value)}><option value="">Choose a local paper account</option>{accounts.map((item) => <option value={item.account_id} key={item.account_id}>{item.display_name} · {item.base_ccy}</option>)}</select></Field>
          <Field label="New binding mode"><select value={mode} onChange={(event) => setMode(event.target.value)}>{["off", "close_only", "trading"].map((item) => <option key={item}>{item}</option>)}</select></Field>
        </div><p>{modeConsequence[mode]}</p><button className="button button-primary" disabled={locked || !strategyId || !accountId}>Review new binding</button></form>
      </Panel>
      <Panel title="Binding modes and risk" subtitle="Risk values are stored fractions. Every changed field includes its expected current value.">
        {data.control.bindings === null ? <p role="status">Paper bindings unavailable.</p> : !data.control.bindings.length ? <p>No paper strategy bindings.</p> : data.control.bindings.map((binding) => {
          const strategy = data.strategies?.strategies.find((item) => item.strategy_id === binding.strategy_id);
          const recorded = strategy?.bindings.find((item) => item.binding_id === binding.binding_id && item.account_id === binding.account_id);
          return <PaperBinding key={`${binding.binding_id}:${JSON.stringify(binding)}`} binding={binding} disabled={locked} modes={data.control.modes} readiness={recorded?.readiness} readinessAsOf={data.strategies?.as_of} onReview={(_, expected, changes) => reviewUpdate(binding.binding_id, expected, changes)} />;
        })}
        <p>{modeConsequence.off} {modeConsequence.close_only}</p>
        <details><summary>Server risk limits</summary><JsonView value={data.control.limits as unknown as import("@/lib/research-types").Json} /></details>
      </Panel>
      <Panel title="Recorded balances and positions" subtitle="Currency and observation time stay attached to each value.">
        {data.pnl === null ? <p role="status">Paper balances and positions unavailable.</p> : !data.pnl.accounts.length ? <p>No recorded balance or position observations.</p> : data.pnl.accounts.map((item) => <article key={item.account_id} className="research-binding"><h3>{item.name} · {item.currency}</h3><p>Latest equity: {formatMoney(item.equity?.value, item.currency)} · <RecordedTime value={item.equity?.as_of} /> · Source {item.equity?.source ?? "unavailable"}</p><p>Realized P&amp;L: {formatMoney(item.realized_total, item.currency)}</p>{item.positions === null ? <p>Positions unavailable.</p> : !item.positions.length ? <p>No recorded open positions.</p> : <div className="table-scroll"><table className="data-table"><thead><tr><th>INSTRUMENT</th><th>QUANTITY</th><th>AVERAGE PRICE</th><th>LAST MARK</th><th>OBSERVED</th></tr></thead><tbody>{item.positions.map((position, index) => <tr key={`${position.symbol}-${index}`}><td>{position.symbol}</td><td>{position.quantity}</td><td>{formatAmount(position.average_price)}{position.average_price !== null && " · Quote currency unavailable"}</td><td>{formatAmount(position.last_mark)}{position.last_mark !== null && " · Quote currency unavailable"}</td><td><RecordedTime value={position.as_of} /></td></tr>)}</tbody></table></div>}</article>)}
      </Panel>
      <Panel title="Recorded paper fills" subtitle={data.fills ? `Observed ${data.fills.as_of}` : "Fill observations"}>
        {data.fills === null ? <p>Paper fills unavailable.</p> : !data.fills.fills.length ? <p>No recorded paper fills.</p> : <div className="table-scroll"><table className="data-table"><thead><tr><th>RECORDED TIME</th><th>ACCOUNT</th><th>INSTRUMENT</th><th>SIDE / QUANTITY</th><th>PRICE</th><th>FEE</th></tr></thead><tbody>{data.fills.fills.map((fill) => <tr key={fill.id}><td><RecordedTime value={fill.at} /></td><td>{fill.account_id}</td><td>{fill.symbol}</td><td>{fill.side} · {fill.quantity}</td><td>{formatMoney(fill.price, fill.currency)}</td><td>{formatMoney(fill.fee, fill.fee_currency)}</td></tr>)}</tbody></table></div>}
      </Panel>
    </>}
    {confirmation && <ConfirmationDialog {...confirmation} busy={busy} disabled={locked} onCancel={() => setConfirmation(null)} onConfirm={confirm} />}
  </>;
}
