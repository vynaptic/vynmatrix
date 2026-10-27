import type { OverviewData } from "@/lib/types";

type ActivityEvent = OverviewData["activity"][number];

export function keyedActivity(events: readonly ActivityEvent[]): Array<{ key: string; event: ActivityEvent }> {
  const occurrences = new Map<string, number>();
  return events.map((event) => {
    // Older overview responses omit signal ids. Include every observable event field
    // so simultaneous signals remain distinct, then number only identical occurrences.
    const identity = event.id !== undefined
      ? JSON.stringify([event.kind, event.id])
      : JSON.stringify([
        event.kind, event.at, event.strategy_id, event.symbol, event.action, event.confidence,
        event.account_id, event.side, event.quantity, event.price, event.currency,
        event.fee, event.fee_currency, event.venue,
      ]);
    const occurrence = occurrences.get(identity) ?? 0;
    occurrences.set(identity, occurrence + 1);
    return { key: `${identity}:${occurrence}`, event };
  });
}
