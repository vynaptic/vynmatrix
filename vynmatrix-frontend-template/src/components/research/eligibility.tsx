import type { Dataset, JsonObject } from "@/lib/research-types";

function evidence(dataset: Dataset | undefined): JsonObject | null {
  const value = dataset?.eligibility;
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}
export function datasetEligible(dataset: Dataset | undefined, purpose: "training" | "backtest"): boolean {
  return evidence(dataset)?.[purpose] === true;
}
export function DatasetEligibility({ dataset, purpose }: { dataset: Dataset | undefined; purpose: "training" | "backtest" }) {
  const value = evidence(dataset);
  const reasons = value?.[`${purpose}_refusals`];
  return <div className="research-notice">
    <strong>{datasetEligible(dataset, purpose) ? `Dataset eligible for ${purpose}` : `Dataset ineligible for ${purpose}`}</strong>
    {Array.isArray(reasons) && reasons.length > 0 && <ul>{reasons.map((reason, index) => <li key={index}>{String(reason)}</li>)}</ul>}
    {!value && <p>Eligibility evidence is unavailable. Refresh the workspace.</p>}
    {typeof value?.provenance_strength === "string" && <span>Provenance strength: {value.provenance_strength}</span>}
  </div>;
}
