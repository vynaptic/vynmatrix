"use client";

import { useRef, useState, type FormEvent } from "react";
import { PageHeader, Panel } from "@/components/ui";
import { researchApi } from "@/lib/research-api";
import { DatasetEligibility, datasetEligible } from "@/components/research/eligibility";
import { downloadJson, errorMessage, Feedback, Field, JobDetails, JobList, JsonView, RecordedTime, requestId, Supervisor, useResearchJob, useWorkspace, WorkspaceMore } from "@/components/research/common";

export default function MlLabPage() {
  const workspace = useWorkspace();
  const controller = useResearchJob();
  const [datasetId, setDatasetId] = useState("");
  const [horizon, setHorizon] = useState("1");
  const [modelName, setModelName] = useState("");
  const [modelId, setModelId] = useState("");
  const [predictionDataset, setPredictionDataset] = useState("");
  const [evaluationStart, setEvaluationStart] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const registration = useRef<{ key: string; id: string } | null>(null);
  const available = Boolean(workspace.data && !workspace.error && !workspace.data.supervisor.maintenance);
  async function train(event: FormEvent) {
    event.preventDefault(); setMessage(null);
    await controller.enqueue("train", { dataset_id: datasetId, horizon: Number(horizon) });
    workspace.refresh();
  }
  async function predict(event: FormEvent) {
    event.preventDefault(); setMessage(null);
    await controller.enqueue("predict", { model_id: modelId, dataset_id: predictionDataset, ...(evaluationStart ? { evaluation_start: evaluationStart } : {}) });
    workspace.refresh();
  }
  async function register(event: FormEvent) {
    event.preventDefault(); if (!controller.job || !modelName.trim()) return;
    const key = `${controller.job.job_id}:${modelName.trim()}`;
    if (registration.current?.key !== key) registration.current = { key, id: requestId() };
    setBusy(true); setError(null);
    try {
      const result = await researchApi.register({ request_id: registration.current.id, job_id: controller.job.job_id, name: modelName.trim() });
      setMessage(`Model registered: ${result.model.name}`); registration.current = null; workspace.refresh();
    } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  }
  async function exportModel(id: string) {
    try { downloadJson(`model-${id}.json`, await researchApi.exportModel(id)); } catch (failure) { setError(errorMessage(failure)); }
  }
  return <>
    <PageHeader eyebrow="RECORDED-DATA RESEARCH" title="ML Intelligence" description="Fit deterministic return models with chronological validation and an untouched test window." actions={<button className="button secondary" onClick={workspace.refresh}>Refresh workspace</button>} />
    <Feedback error={workspace.error || error} message={message} />
    {workspace.loading && <p role="status">Loading research workspace…</p>}
    {workspace.data && <>
      <Supervisor workspace={workspace.data} />
      <Panel title="Train a return model" subtitle="L2 regression · horizon 1–30 observed bars · causal features · 60/20/20 chronological split">
        <p>Requires at least 1,000 usable labeled observations and 200 final test observations. Scaling uses training data; validation chooses regularization. Forecast errors are not trading P&amp;L.</p>
        <form onSubmit={train}><div className="form-grid">
          <Field label="Training dataset"><select required value={datasetId} onChange={(event) => setDatasetId(event.target.value)}><option value="">Choose a frozen dataset</option>{workspace.data.datasets.map((dataset) => <option key={dataset.dataset_id} value={dataset.dataset_id}>{dataset.name} · {dataset.metadata.currency} · {dataset.bar_count} bars</option>)}</select></Field>
          <Field label="Forecast horizon"><input required type="number" min="1" max="30" step="1" value={horizon} onChange={(event) => setHorizon(event.target.value)} /></Field>
        </div>
        {datasetId && <DatasetEligibility dataset={workspace.data.datasets.find((dataset) => dataset.dataset_id === datasetId)} purpose="training" />}
        {!workspace.data.datasets.length && <p>No frozen datasets. Import recorded data in Market Data first.</p>}
        <WorkspaceMore workspace={workspace} directory="datasets" /><button className="button primary" disabled={!available || controller.busy || !datasetEligible(workspace.data.datasets.find((dataset) => dataset.dataset_id === datasetId), "training") || !Number.isInteger(Number(horizon)) || Number(horizon) < 1 || Number(horizon) > 30}>Train model</button></form>
      </Panel>
      <JobDetails controller={controller} />
      {controller.job?.status === "completed" && controller.job.kind === "train" && <Panel title="Register fitted model" subtitle="Freezes the model JSON, earliest allowed evaluation time and dataset/split/image identity."><form className="research-toolbar" onSubmit={register}><Field label="Model name"><input required value={modelName} onChange={(event) => setModelName(event.target.value)} maxLength={255} /></Field><button className="button primary" disabled={busy || !modelName.trim()}>Register model</button></form></Panel>}
      <Panel title="Registered models" subtitle="A registration does not release or authorize paper execution.">
        {!workspace.data.models.length ? <p>No registered models.</p> : <div className="research-list">{workspace.data.models.map((model) => <div className="research-list-item" key={model.model_id}><div><strong>{model.name}</strong><p>{model.model_id} · Created <RecordedTime value={model.created_at} /></p><p>Earliest allowed evaluation: <RecordedTime value={model.training_cutoff} /></p><JsonView value={model.metrics} label={`${model.name} forecast metrics`} /></div><button className="button secondary small" onClick={() => exportModel(model.model_id)}>Download {model.name}</button></div>)}</div>}
      </Panel>
      <WorkspaceMore workspace={workspace} directory="models" />
      <Panel title="Recorded-data prediction" subtitle="Evaluation may start at or after the model's earliest allowed evaluation time. Holdout reuse stays recorded.">
        <form onSubmit={predict}><div className="form-grid">
          <Field label="Prediction model"><select required value={modelId} onChange={(event) => setModelId(event.target.value)}><option value="">Choose a frozen model</option>{workspace.data.models.map((model) => <option value={model.model_id} key={model.model_id}>{model.name}</option>)}</select></Field>
          <Field label="Prediction dataset"><select required value={predictionDataset} onChange={(event) => setPredictionDataset(event.target.value)}><option value="">Choose a frozen dataset</option>{workspace.data.datasets.map((dataset) => <option value={dataset.dataset_id} key={dataset.dataset_id}>{dataset.name}</option>)}</select></Field>
          <Field label="Prediction evaluation start"><input value={evaluationStart} onChange={(event) => setEvaluationStart(event.target.value)} placeholder="Optional UTC ISO timestamp" /></Field>
        </div><button className="button primary" disabled={!available || controller.busy || !modelId || !predictionDataset}>Run predictions</button></form>
      </Panel>
      <JobList workspace={workspace.data} controller={controller} kinds={["train", "predict"]} pagination={workspace} />
      <details><summary>Workspace eligibility and limits</summary><JsonView value={workspace.data.eligibility} /><JsonView value={workspace.data.limits} /></details>
    </>}
  </>;
}
