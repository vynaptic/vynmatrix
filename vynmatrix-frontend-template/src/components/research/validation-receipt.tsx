import type { ImportReceipt, Json } from "@/lib/research-types";
import { JsonView, RecordedTime } from "./common";

export function ValidationReceipt({ receipt, origin, window }: { receipt: ImportReceipt; origin: "import" | "snapshot"; window?: { start: string; end: string } }) {
  return <div className="research-receipt">
    <h3>{receipt.valid ? "Valid" : "Invalid"} {origin} receipt</h3>
    <p>Receipt <code>{receipt.import_id}</code> · Recorded <RecordedTime value={receipt.created_at} /></p>
    {window && <p>Requested window: <RecordedTime value={window.start} /> to <RecordedTime value={window.end} /></p>}
    <p>Content hash: <code>{receipt.content_hash ?? "Not available for invalid content"}</code></p>
    <p>Original hash: <code>{receipt.original_hash ?? "Not recorded"}</code></p>
    <JsonView value={receipt.report} label={`${origin === "snapshot" ? "Snapshot" : "Import"} validation report`} />
    <details><summary>Receipt metadata</summary><JsonView value={receipt.metadata as unknown as Json} /></details>
  </div>;
}
