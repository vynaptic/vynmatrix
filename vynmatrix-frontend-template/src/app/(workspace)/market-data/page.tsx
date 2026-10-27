"use client";

import { useCallback, useRef, useState, type FormEvent } from "react";
import { PageHeader, Panel } from "@/components/ui";
import { ValidationReceipt } from "@/components/research/validation-receipt";
import { researchApi } from "@/lib/research-api";
import type { ImportReceipt, ResearchBar, ResearchMetadata } from "@/lib/research-types";
import { downloadJson, errorMessage, Feedback, Field, JsonView, RecordedTime, useResource } from "@/components/research/common";

const initialMetadata: ResearchMetadata = { instrument_id: 0, source: "", provenance: "", currency: "", timeframe: "", timestamp_semantics: "bar_open", adjustment_policy: "raw" };
function readCsv(file: File): Promise<string> {
  if (file.size > 10 * 1024 * 1024) return Promise.reject(new Error("CSV exceeds the 10 MiB upload limit."));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Could not read the CSV file."));
    reader.onload = () => {
      try { resolve(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(reader.result as ArrayBuffer)); }
      catch { reject(new Error("CSV must be valid UTF-8.")); }
    };
    reader.readAsArrayBuffer(file);
  });
}
export default function MarketDataPage() {
  const [query, setQuery] = useState({ search: "", instrument_id: "", source: "", timeframe: "" });
  const [filter, setFilter] = useState("limit=100");
  const loader = useCallback((signal: AbortSignal) => researchApi.market(new URLSearchParams(filter), signal), [filter]);
  const market = useResource(loader);
  const [datasetsAfter, setDatasetsAfter] = useState<string | undefined>(undefined);
  const datasetsLoader = useCallback((signal: AbortSignal) => researchApi.datasets(signal, datasetsAfter), [datasetsAfter]);
  const datasets = useResource(datasetsLoader);
  const [metadata, setMetadata] = useState(initialMetadata);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [receipt, setReceipt] = useState<ImportReceipt | null>(null);
  const [snapshotReceipt, setSnapshotReceipt] = useState<ImportReceipt | null>(null);
  const [snapshotWindow, setSnapshotWindow] = useState<{ start: string; end: string } | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [selected, setSelected] = useState("");
  const [after, setAfter] = useState<number | undefined>(undefined);
  const version = useRef(0);
  const detailLoader = useCallback((signal: AbortSignal) => selected ? researchApi.dataset(selected, signal, after) : Promise.resolve(null), [selected, after]);
  const detail = useResource(detailLoader);
  const validMetadata = metadata.instrument_id > 0 && [metadata.source, metadata.provenance, metadata.currency, metadata.timeframe].every((value) => value.trim());
  function editMetadata<K extends keyof ResearchMetadata>(key: K, value: ResearchMetadata[K]) {
    version.current++; setMetadata((old) => ({ ...old, [key]: value })); setReceipt(null); setMessage(null);
  }
  async function validate(event: FormEvent) {
    event.preventDefault(); if (!file || !validMetadata) return;
    const current = ++version.current; setBusy(true); setError(null); setReceipt(null); setMessage(null);
    try {
      const csv = await readCsv(file);
      const result = await researchApi.importCsv(metadata, csv);
      if (current === version.current) setReceipt(result.import);
    } catch (failure) { if (current === version.current) setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  async function freeze() {
    if (!receipt?.valid || !receipt.content_hash || !name.trim()) return;
    setBusy(true); setError(null);
    try { const result = await researchApi.freeze(receipt.import_id, receipt.content_hash, name.trim()); setMessage(`Dataset frozen: ${result.dataset.name}`); datasets.refresh(); }
    catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  }
  async function snapshot(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null); setMessage(null); setSnapshotReceipt(null);
    try {
      if (![start, end].every((value) => /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))) || Date.parse(start) >= Date.parse(end)) throw new Error("Snapshot requires an increasing window with explicit UTC or offset timestamps.");
      const result = await researchApi.snapshot({ ...metadata, start, end }, name.trim());
      if (result.dataset) { setMessage(`Dataset frozen: ${result.dataset.name}`); datasets.refresh(); }
      else { setSnapshotReceipt(result.import); setSnapshotWindow({ start, end }); }
    } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  }
  async function exportDataset() {
    if (!selected) return;
    try { downloadJson(`dataset-${selected}.json`, await researchApi.exportDataset(selected)); }
    catch (failure) { setError(errorMessage(failure)); }
  }
  function applyFilters(event: FormEvent) {
    event.preventDefault(); const params = new URLSearchParams({ limit: "100" });
    for (const [key, value] of Object.entries(query)) if (value.trim()) params.set(key, value.trim());
    setFilter(params.toString()); market.refresh();
  }
  return <>
    <PageHeader eyebrow="RECORDED DATA" title="Market Data" description="Browse recorded bars and freeze immutable research datasets with explicit provenance." actions={<button className="button secondary" onClick={() => { market.refresh(); datasets.refresh(); }}>Refresh data</button>} />
    <Feedback error={error} message={message} />
    <Panel title="Recorded market" subtitle={market.data ? `Observed ${market.data.as_of}` : "Owner catalogue and recorded feeds"}>
      <Feedback error={market.error} />
      <form className="research-toolbar" onSubmit={applyFilters}>
        <Field label="Search instruments"><input value={query.search} onChange={(event) => setQuery({ ...query, search: event.target.value })} /></Field>
        <Field label="Market instrument"><select value={query.instrument_id} onChange={(event) => setQuery({ ...query, instrument_id: event.target.value })}><option value="">All instruments</option>{market.data?.instruments.map((instrument) => <option value={instrument.instrument_id} key={instrument.instrument_id}>{instrument.symbol} · {instrument.asset_class} · {instrument.currency ?? "Currency unavailable"}</option>)}</select></Field>
        <Field label="Market source"><input value={query.source} onChange={(event) => setQuery({ ...query, source: event.target.value })} /></Field>
        <Field label="Market timeframe"><input value={query.timeframe} onChange={(event) => setQuery({ ...query, timeframe: event.target.value })} /></Field>
        <button className="button secondary" type="submit">Apply filters</button>
      </form>
      {market.loading ? <p role="status">Loading recorded market data…</p> : market.data && <>
        <details><summary>Instrument catalogue ({market.data.instruments.length})</summary><JsonView value={market.data.instruments as unknown as import("@/lib/research-types").Json} /></details>
        <details><summary>Recorded feeds ({market.data.feeds.length})</summary><JsonView value={market.data.feeds} /></details>
        {["instrument_id", "source", "timeframe"].every((key) => new URLSearchParams(filter).get(key)) ? <Bars bars={market.data.bars} /> : <p>Select an instrument, source and timeframe to browse its recorded bars.</p>}
        {market.data.next_before && <button className="button secondary small" onClick={() => { const params = new URLSearchParams(filter); params.set("before", market.data!.next_before!); setFilter(params.toString()); }}>{new URLSearchParams(filter).get("instrument_id") ? "Older bars" : "More instruments"}</button>}
      </>}
    </Panel>
    <Panel title="Import recorded CSV" subtitle="10 MiB / 100,000 bars maximum. Uploads remain separate from runtime prices.">
      <p>Header: <code>ts,open,high,low,close,volume</code>. Optional timing columns must include both <code>bar_open,bar_close</code>. Timestamps need an explicit UTC or offset. Session instruments require authoritative calendar evidence.</p>
      <form onSubmit={validate}>
        <fieldset disabled={busy} className="research-fieldset"><div className="form-grid">
          <Field label="Import instrument"><select required value={metadata.instrument_id || ""} onChange={(event) => editMetadata("instrument_id", Number(event.target.value))}><option value="">Choose an existing instrument</option>{market.data?.instruments.map((instrument) => <option value={instrument.instrument_id} key={instrument.instrument_id}>{instrument.symbol} · {instrument.asset_class}</option>)}</select></Field>
          <Field label="Import source"><input required value={metadata.source} onChange={(event) => editMetadata("source", event.target.value)} /></Field>
          <Field label="Import currency"><input required value={metadata.currency} onChange={(event) => editMetadata("currency", event.target.value.toUpperCase())} /></Field>
          <Field label="Import timeframe"><input required value={metadata.timeframe} placeholder="Recorded feed timeframe" onChange={(event) => editMetadata("timeframe", event.target.value)} /></Field>
          <Field label="Timestamp semantics"><select value={metadata.timestamp_semantics} onChange={(event) => editMetadata("timestamp_semantics", event.target.value as ResearchMetadata["timestamp_semantics"])}><option value="bar_open">Bar open</option><option value="bar_close">Bar close</option></select></Field>
          <Field label="Adjustment policy"><select value={metadata.adjustment_policy} onChange={(event) => editMetadata("adjustment_policy", event.target.value as ResearchMetadata["adjustment_policy"])}><option value="raw">Raw</option><option value="adjusted">Adjusted</option></select></Field>
          <Field label="Import provenance"><input required value={metadata.provenance} onChange={(event) => editMetadata("provenance", event.target.value)} /></Field>
          <Field label="Dataset name"><input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} /></Field>
          <Field label="CSV file"><input type="file" accept=".csv,text/csv" onChange={(event) => { version.current++; setFile(event.target.files?.[0] ?? null); setReceipt(null); setMessage(null); }} /></Field>
        </div></fieldset>
        <div className="research-toolbar"><button className="button primary" disabled={busy || !file || !validMetadata} type="submit">Validate CSV</button><button className="button secondary" type="button" disabled={busy || !receipt?.valid || !receipt.content_hash || !name.trim()} onClick={freeze}>Freeze dataset</button></div>
      </form>
      {receipt && <ValidationReceipt receipt={receipt} origin="import" />}
      <p className="micro-copy">Structural validity proves content identity. Independent market provenance and historical availability remain separate evidence.</p>
    </Panel>
    <Panel title="Snapshot stored prices" subtitle="Uses the instrument, source, currency, timeframe and provenance above; freezes the exact stored values.">
      <form onSubmit={snapshot}><fieldset className="research-fieldset" disabled={busy}><div className="form-grid">
        <Field label="Snapshot start"><input required value={start} placeholder="UTC ISO timestamp" onChange={(event) => setStart(event.target.value)} /></Field>
        <Field label="Snapshot end"><input required value={end} placeholder="UTC ISO timestamp" onChange={(event) => setEnd(event.target.value)} /></Field>
      </div><button className="button secondary" disabled={busy || !validMetadata || !name.trim() || !start || !end}>Freeze stored window</button></fieldset></form>
      {snapshotReceipt && <ValidationReceipt receipt={snapshotReceipt} origin="snapshot" window={snapshotWindow} />}
    </Panel>
    <Panel title="Frozen datasets" subtitle="Immutable manifests retain terms, time semantics, provenance and eligibility.">
      <Feedback error={datasets.error || detail.error} />
      {datasets.loading ? <p>Loading datasets…</p> : datasets.data?.datasets.length === 0 ? <p>No frozen datasets. Validate and freeze a recorded CSV or stored window.</p> : null}
      <div className="research-toolbar"><Field label="Browse dataset"><select value={selected} onChange={(event) => { setSelected(event.target.value); setAfter(undefined); }}><option value="">Choose a frozen dataset</option>{datasets.data?.datasets.map((dataset) => <option key={dataset.dataset_id} value={dataset.dataset_id}>{dataset.name} · {dataset.metadata.currency} · {dataset.bar_count} bars</option>)}</select></Field><button className="button secondary" disabled={!selected} onClick={exportDataset}>Export dataset</button>{datasetsAfter && <button className="button secondary small" onClick={() => { setDatasetsAfter(undefined); setSelected(""); }}>Latest datasets</button>}{datasets.data?.next_after && <button className="button secondary small" disabled={datasets.loading} onClick={() => { setDatasetsAfter(datasets.data!.next_after!); setSelected(""); }}>Older datasets</button>}</div>
      {detail.data && detail.data.dataset.dataset_id === selected && <><h3>{detail.data.dataset.name}</h3><p>Frozen <RecordedTime value={detail.data.dataset.created_at} /> · {detail.data.dataset.metadata.currency} · {detail.data.dataset.bar_count} bars</p><JsonView value={detail.data.dataset.metadata as unknown as import("@/lib/research-types").Json} label="Dataset metadata" /><JsonView value={detail.data.dataset.eligibility} label="Dataset eligibility" /><details><summary>Frozen manifest and hashes</summary><JsonView value={detail.data.dataset.manifest} /><p>{detail.data.dataset.content_hash}</p></details><Bars bars={detail.data.bars} currency={detail.data.dataset.metadata.currency} /><div className="research-toolbar">{after !== undefined && <button className="button secondary small" onClick={() => setAfter(undefined)}>First dataset bars</button>}{detail.data.next_after !== null && detail.data.next_after !== undefined && <button className="button secondary small" onClick={() => setAfter(detail.data!.next_after!)}>Next dataset bars</button>}</div></>}
    </Panel>
  </>;
}
function Bars({ bars, currency }: { bars: ResearchBar[]; currency?: string }) {
  const timing = bars.some((bar) => bar.bar_open || bar.bar_close);
  const identity = bars.some((bar) => bar.symbol || bar.source || bar.timeframe);
  return bars.length ? <div className="table-scroll"><table className="data-table"><thead><tr>{identity && <th>INSTRUMENT / FEED</th>}{["RECORDED TIME", "CURRENCY", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"].map((title) => <th key={title}>{title}</th>)}{timing && <><th>BAR OPEN</th><th>BAR CLOSE</th></>}</tr></thead><tbody>{bars.map((bar, index) => <tr key={`${bar.ts}-${index}`}>
    {identity && <td>{bar.symbol ?? bar.instrument_id ?? "Identity unavailable"}<span className="table-subline">{bar.source ?? "Source unavailable"} · {bar.timeframe ?? "Timeframe unavailable"}</span></td>}
    <td><RecordedTime value={bar.ts} /></td><td>{bar.currency ?? currency ?? "Currency unavailable"}</td><td>{bar.open}</td><td>{bar.high}</td><td>{bar.low}</td><td>{bar.close}</td><td>{bar.volume}</td>{timing && <><td><RecordedTime value={bar.bar_open} /></td><td><RecordedTime value={bar.bar_close} /></td></>}
  </tr>)}</tbody></table></div> : <p>No bars in this recorded window.</p>;
}
