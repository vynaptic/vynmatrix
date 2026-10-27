"use client";

import { useState } from "react";
import type { BindingReadiness, ControlData } from "@/lib/types";
import type { JsonObject } from "@/lib/research-types";
import { Field } from "./common";
import { RecordedBindingReadiness } from "./binding-readiness";

export type Binding = NonNullable<ControlData["bindings"]>[number];
const riskLabels = { asset_score_threshold: "Score threshold", max_position_pct: "Maximum position fraction", max_total_exposure_pct: "Maximum exposure fraction", max_daily_loss_pct: "Maximum daily loss fraction", max_open_positions: "Maximum open positions" };
export function PaperBinding({ binding, disabled, modes, onReview, readiness, readinessAsOf }: { binding: Binding; disabled: boolean; modes: string[]; readiness?: BindingReadiness | null; readinessAsOf?: string; onReview: (binding: Binding, expected: JsonObject, changes: JsonObject) => void }) {
  const [mode, setMode] = useState(binding.mode);
  const [risk, setRisk] = useState(Object.fromEntries(Object.keys(riskLabels).map((key) => [key, String(binding[key as keyof Binding])])));
  function review() {
    const expected: JsonObject = {}; const changes: JsonObject = {};
    if (mode !== binding.mode) { expected.mode = binding.mode; changes.mode = mode; }
    for (const key of Object.keys(riskLabels)) {
      const old = binding[key as keyof Binding];
      const next = key === "max_open_positions" ? Number(risk[key]) : risk[key];
      if (String(old) !== String(next)) { expected[key] = old as string | number; changes[key] = next; }
    }
    if (Object.keys(changes).length) onReview(binding, expected, changes);
  }
  const changed = mode !== binding.mode || Object.keys(riskLabels).some((key) => risk[key] !== String(binding[key as keyof Binding]));
  return <article className="research-binding">
    <h3>{binding.strategy_id} · Binding {binding.binding_id} · Account {binding.account_id}</h3>
    <RecordedBindingReadiness bindingId={binding.binding_id} readiness={readiness} asOf={readinessAsOf} />
    <div className="form-grid"><Field label={`Binding ${binding.binding_id} mode`}><select disabled={disabled} value={mode} onChange={(event) => setMode(event.target.value)}>{!modes.includes(binding.mode) && <option value={binding.mode}>{binding.mode} (stored)</option>}{modes.filter((mode) => ["off", "close_only", "trading"].includes(mode)).map((value) => <option key={value} value={value}>{value}</option>)}</select></Field>
      {Object.entries(riskLabels).map(([key, label]) => <Field key={key} label={`Binding ${binding.binding_id} ${label.toLowerCase()}`}><input disabled={disabled} value={risk[key]} inputMode="decimal" onChange={(event) => setRisk((old) => ({ ...old, [key]: event.target.value }))} /></Field>)}
    </div>
    <p>Instrument scope: {binding.instruments_allowed?.join(", ") ?? "All eligible instruments"} · Asset classes: {binding.asset_classes_allowed.join(", ")}</p>
    <button className="button button-secondary" disabled={disabled || !changed || Object.values(risk).some((value) => !value.trim())} onClick={review}>Review binding {binding.binding_id}</button>
  </article>;
}
export const modeConsequence: Record<string, string> = {
  off: "Off disables entries and exits. Existing positions remain open; this does not liquidate them.",
  close_only: "Close only permits exits and disables entries.",
  trading: "Trading enables automated paper entries and exits, subject to release, instrument and risk gates.",
};
