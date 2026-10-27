"use client";

import { useRef, useState, type FormEvent } from "react";
import { PageHeader, Panel } from "@/components/ui";
import { researchApi } from "@/lib/research-api";
import { DatasetEligibility, datasetEligible } from "@/components/research/eligibility";
import { ConfirmationDialog } from "@/components/research/confirmation-dialog";
import type { JsonObject, ResearchDraft, ResearchTemplate } from "@/lib/research-types";
import { downloadJson, errorMessage, Feedback, Field, isConflict, JobDetails, JobList, requestId, Supervisor, useResearchJob, useWorkspace, WorkspaceMore, useResource } from "@/components/research/common";

const emptyConfig = JSON.stringify({ class_name: "UserStrategy", strategy_id: "", parameters: {} }, null, 2);
const emptyAssumptions = { initial_capital: "", currency: "", size_pct: "", fee_bps: "", slippage_bps: "", annualization_factor: "" };
export default function StrategyNewPage() {
  const workspace = useWorkspace();
  const controller = useResearchJob();
  const templates = useResource(researchApi.templates);
  const [templateId, setTemplateId] = useState("");
  const [pendingTemplate, setPendingTemplate] = useState<ResearchTemplate | null>(null);
  const [requiresModel, setRequiresModel] = useState(false);
  const [saved, setSaved] = useState<ResearchDraft | null>(null);
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  const [config, setConfig] = useState(emptyConfig);
  const [datasetId, setDatasetId] = useState("");
  const [modelId, setModelId] = useState("");
  const [evaluationStart, setEvaluationStart] = useState("");
  const [assumptions, setAssumptions] = useState(emptyAssumptions);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const loadVersion = useRef(0);
  const pendingSave = useRef<{ key: string; id: string } | null>(null);
  const dirty = saved ? name !== saved.name || source !== saved.source || config !== JSON.stringify(saved.config, null, 2) : Boolean(name || source || config !== emptyConfig);
  const modelRequired = requiresModel || Boolean(templates.data?.templates.some((template) => template.requires_model && template.source === source));
  const available = Boolean(workspace.data && !workspace.error && !workspace.data.supervisor.maintenance);
  async function loadDraft(id: string) {
    const current = ++loadVersion.current; setBusy(true); setError(null); setMessage(null);
    try {
      const result = await researchApi.draft(id);
      if (current === loadVersion.current) { setSaved(result.draft); setName(result.draft.name); setSource(result.draft.source); setConfig(JSON.stringify(result.draft.config, null, 2)); setRequiresModel(templates.data?.templates.find((template) => template.source === result.draft.source)?.requires_model ?? false); setConflict(false); }
    } catch (failure) { if (current === loadVersion.current) setError(errorMessage(failure)); }
    finally { if (current === loadVersion.current) setBusy(false); }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null); setMessage(null);
    try {
      const parsed: unknown = JSON.parse(config);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Strategy config must be a JSON object.");
      const parameters = (parsed as JsonObject).parameters;
      if (parameters && typeof parameters === "object" && !Array.isArray(parameters) && Object.hasOwn(parameters, "research_model")) throw new Error("Select a registered model for the run; research_model is supplied by the trusted runner.");
      JSON.stringify(parsed, (_key, value: unknown) => { if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Config numbers must be finite."); return value; });
      if (new TextEncoder().encode(source).length > 256 * 1024) throw new Error("Python source exceeds the 256 KiB limit.");
      const fields = { name: name.trim(), source, config: parsed as JsonObject };
      const key = JSON.stringify(fields);
      if (pendingSave.current?.key !== key) pendingSave.current = { key, id: requestId() };
      const result = saved ? await researchApi.saveRevision(saved.draft_id, { ...fields, expected_revision: saved.revision }) : await researchApi.saveDraft({ ...fields, request_id: pendingSave.current.id });
      setSaved(result.draft); setName(result.draft.name); setSource(result.draft.source); setConfig(JSON.stringify(result.draft.config, null, 2));
      setConflict(false); setMessage(`Saved revision ${result.draft.revision} · ${result.draft.source_hash}`); pendingSave.current = null; workspace.refresh();
    } catch (failure) { setConflict(isConflict(failure)); setError(`${errorMessage(failure)}${isConflict(failure) ? " Your unsaved source is retained. Reload the saved revision before editing again." : ""}`); }
    finally { setBusy(false); }
  }
  async function backtest(event: FormEvent) {
    event.preventDefault(); if (!saved || dirty || (modelRequired && !modelId)) return;
    setMessage(null);
    await controller.enqueue("backtest", { revision_id: saved.revision_id, dataset_id: datasetId, ...(modelId ? { model_id: modelId } : {}), ...(evaluationStart ? { evaluation_start: evaluationStart } : {}), assumptions });
    workspace.refresh();
  }
  async function exportDraft() {
    if (!saved) return;
    try { downloadJson(`strategy-${saved.draft_id}-revision-${saved.revision}.json`, await researchApi.exportDraft(saved.draft_id, saved.revision_id)); }
    catch (failure) { setError(errorMessage(failure)); }
  }
  function applyTemplate(template: ResearchTemplate) {
    setSource(template.source); setConfig(JSON.stringify(template.config, null, 2)); setName((old) => old || template.name); setRequiresModel(template.requires_model); setPendingTemplate(null); if (!conflict) setError(null); setMessage(`Loaded ${template.name}. Save a revision before running.`);
  }
  function loadTemplate() {
    const template = templates.data?.templates.find((item) => item.template_id === templateId);
    if (!template) return;
    if (dirty) setPendingTemplate(template); else applyTemplate(template);
  }
  function discardEdits() {
    setName(saved?.name ?? ""); setSource(saved?.source ?? ""); setConfig(saved ? JSON.stringify(saved.config, null, 2) : emptyConfig); setRequiresModel(templates.data?.templates.find((template) => template.source === saved?.source)?.requires_model ?? false); setMessage(null);
  }
  return <>
    <PageHeader eyebrow="HISTORICAL PYTHON RESEARCH" title="Create a strategy" description="Save immutable source revisions, validate in isolation and backtest against frozen recorded data." actions={<button className="button secondary" onClick={workspace.refresh}>Refresh workspace</button>} />
    <Feedback error={workspace.error || error} message={message} />
    {workspace.loading && <p role="status">Loading research workspace…</p>}
    {workspace.data && <>
      <Supervisor workspace={workspace.data} />
      <div className="research-notice">A draft, model or backtest does not package, release, bind or authorize a strategy for paper execution.</div>
      <Panel title="Python strategy revision" subtitle="Implement PureSignalStrategy.initialize() and on_data(state); emit canonical signals with timestamp=state.timestamp.">
        <Feedback error={templates.error} />
        <div className="research-toolbar"><Field label="Strategy template"><select value={templateId} disabled={busy || templates.loading} onChange={(event) => setTemplateId(event.target.value)}><option value="">Choose a canonical starting point</option>{templates.data?.templates.map((template) => <option value={template.template_id} key={template.template_id}>{template.name}</option>)}</select></Field><button className="button secondary" type="button" disabled={busy || templates.loading || !templateId} onClick={loadTemplate}>Load template</button><button className="button secondary small" type="button" disabled={templates.loading} onClick={templates.refresh}>Refresh templates</button></div>
        {modelRequired && <p className="research-notice">This template requires a registered model. Choose its immutable version under Backtest model; the trusted runner supplies the artifact.</p>}
        <div className="research-toolbar"><Field label="Open draft"><select disabled={busy || dirty} value={saved?.draft_id ?? ""} onChange={(event) => { if (event.target.value) void loadDraft(event.target.value); else { setSaved(null); setName(""); setSource(""); setConfig(emptyConfig); setRequiresModel(false); setConflict(false); } }}><option value="">New draft</option>{workspace.data.drafts.map((draft) => <option value={draft.draft_id} key={draft.draft_id}>{draft.name} · revision {draft.revision}</option>)}</select></Field>
          <WorkspaceMore workspace={workspace} directory="drafts" />
          {dirty && <button className="button secondary small" disabled={busy} onClick={discardEdits}>Discard unsaved edits</button>}
          {conflict && saved && <button className="button secondary small" disabled={busy} onClick={() => loadDraft(saved.draft_id)}>Reload saved revision</button>}
        </div>
        <form onSubmit={save}><fieldset disabled={busy} className="research-fieldset">
          <Field label="Draft name"><input required value={name} maxLength={255} onChange={(event) => setName(event.target.value)} /></Field>
          <Field label="Python source"><textarea required className="research-source" spellCheck={false} value={source} onChange={(event) => setSource(event.target.value)} placeholder="Enter your canonical Python strategy source" /></Field>
          <Field label="Strategy config JSON"><textarea required className="research-config" spellCheck={false} value={config} onChange={(event) => setConfig(event.target.value)} /></Field>
          <p>Config requires <code>class_name</code>, your research <code>strategy_id</code> and <code>parameters</code>. Imports and validation execute only in the isolated sandbox. Selecting a frozen model supplies the reserved <code>parameters.research_model</code> value to the run; your source must consume it. Do not set that reserved key in draft config.</p>
        </fieldset><div className="research-toolbar">
          <button className="button primary" disabled={busy || conflict || !name.trim() || !source.trim()}>Save revision</button>
          <button className="button secondary" type="button" disabled={!saved || dirty || busy || !available || controller.busy} onClick={() => { if (saved) void controller.enqueue("validate_strategy", { revision_id: saved.revision_id }); }}>Validate in isolation</button>
          <button className="button secondary" type="button" disabled={!saved} onClick={exportDraft}>Export saved revision</button>
        </div></form>
        {saved && <p>Saved revision {saved.revision}: <code>{saved.revision_id}</code> · <code>{saved.source_hash}</code>{dirty && " · Unsaved edits; save before running."}</p>}
      </Panel>
      <Panel title="Historical backtest" subtitle="Trusted code calculates fills, costs and metrics from validated canonical signals.">
        <form onSubmit={backtest}><div className="form-grid">
          <Field label="Backtest dataset"><select required value={datasetId} onChange={(event) => { setDatasetId(event.target.value); const dataset = workspace.data!.datasets.find((item) => item.dataset_id === event.target.value); setAssumptions((old) => ({ ...old, currency: dataset?.metadata.currency ?? "" })); }}><option value="">Choose a frozen dataset</option>{workspace.data.datasets.map((dataset) => <option value={dataset.dataset_id} key={dataset.dataset_id}>{dataset.name} · {dataset.metadata.currency}</option>)}</select></Field>
          <Field label="Backtest model"><select value={modelId} onChange={(event) => setModelId(event.target.value)}><option value="">No model</option>{workspace.data.models.map((model) => <option value={model.model_id} key={model.model_id}>{model.name} · earliest evaluation {model.training_cutoff}</option>)}</select></Field>
          <Field label="Backtest evaluation start"><input value={evaluationStart} onChange={(event) => setEvaluationStart(event.target.value)} placeholder="Optional UTC ISO timestamp" /></Field>
          {Object.entries(assumptions).map(([key, value]) => <Field label={{ initial_capital: "Research initial capital", currency: "Research quote currency", size_pct: "Position fraction (0–1)", fee_bps: "Research fee (bps)", slippage_bps: "Research slippage (bps)", annualization_factor: "Annualization factor" }[key]!} key={key}><input required value={value} inputMode={key === "currency" ? "text" : "decimal"} onChange={(event) => setAssumptions((old) => ({ ...old, [key]: event.target.value }))} /></Field>)}
        </div>
          {datasetId && <DatasetEligibility dataset={workspace.data.datasets.find((dataset) => dataset.dataset_id === datasetId)} purpose="backtest" />}
          <p>These are research cost assumptions. Supported execution is fully funded long-only crypto spot, cash equities and cash ETFs in the recorded quote currency. Missing product, session or FX evidence is refused.</p>
          <WorkspaceMore workspace={workspace} directory="datasets" /><WorkspaceMore workspace={workspace} directory="models" /><button className="button primary" disabled={!saved || dirty || !available || controller.busy || (modelRequired && !modelId) || !datasetEligible(workspace.data.datasets.find((dataset) => dataset.dataset_id === datasetId), "backtest") || Object.values(assumptions).some((value) => !value.trim())}>Run historical backtest</button>
        </form>
      </Panel>
      <JobDetails controller={controller} />
      <JobList workspace={workspace.data} controller={controller} kinds={["validate_strategy", "backtest"]} pagination={workspace} />
    </>}
    {pendingTemplate && <ConfirmationDialog title="Replace editor contents?" confirmLabel="Replace editor contents" description={`Load ${pendingTemplate.name}`} lines={["Unsaved source and config will be replaced."]} consequence="Saved revisions and queued jobs keep their frozen inputs. Save the new editor contents as a revision before running them." busy={busy} disabled={busy} onCancel={() => setPendingTemplate(null)} onConfirm={() => applyTemplate(pendingTemplate)} />}
  </>;
}
