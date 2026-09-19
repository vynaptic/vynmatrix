// Profit and loss: equity over time, recorded realized P&L, positions and the trade blotter.

import { divergingBars, lineChart } from "../charts.js";
import {
  delta,
  empty,
  fmtDate,
  fmtDateTime,
  fmtMoney,
  fmtNumber,
  fmtPercent,
  fmtQuantity,
  h,
  section,
  table,
  titleCase,
  toNumber,
} from "../format.js";

const RANGES = [30, 90, 365];
const PAGE_SIZE = 50;
const MAX_ROWS = 1000;
// Trades already loaded survive a refresh; `next` is the cursor for older ones.
const local = { days: 90, rows: [], next: null };

export async function load(api) {
  const [pnl, head] = await Promise.all([
    api.get("pnl", { days: local.days }),
    api.get("fills", { limit: PAGE_SIZE }),
  ]);
  mergeNewest(head);
  return { pnl, fills: { rows: local.rows, more: local.next !== null } };
}

function mergeNewest(head) {
  const known = new Set(local.rows.map((row) => row.id));
  const joins = head.fills.some((row) => known.has(row.id));
  if (!local.rows.length || !joins) {
    // First load, or so much new activity that the loaded pages no longer join up.
    local.rows = head.fills;
    local.next = head.next_before;
    return;
  }
  const fresh = head.fills.filter((row) => !known.has(row.id));
  local.rows = [...fresh, ...local.rows].slice(0, MAX_ROWS);
}

async function loadOlder(context) {
  try {
    const page = await context.api.get("fills", { limit: PAGE_SIZE, before: local.next });
    local.rows = [...local.rows, ...page.fills].slice(0, MAX_ROWS);
    local.next = local.rows.length >= MAX_ROWS ? null : page.next_before;
  } finally {
    // A refused or failed request is reported by the normal render path.
    context.refresh();
  }
}

function asTable(columns, rows) {
  return h("details", { class: "as-table" }, h("summary", { text: "Show as a table" }), table(columns, rows));
}

function equityPanel(account) {
  const daily = (account.equity_daily || [])
    .map((point) => ({ label: fmtDate(point.date), value: toNumber(point.value) }))
    .filter((point) => point.value !== null);
  const byTrade = (account.equity_by_trade || [])
    .map((point) => ({
      label: `Trade ${point.trade}, recorded ${fmtDateTime(point.at)}`,
      short: `Trade ${point.trade}`,
      value: toNumber(point.value),
    }))
    .filter((point) => point.value !== null);
  const useDaily = daily.length >= 2;
  const points = useDaily ? daily : byTrade;
  const format = (value) => fmtMoney(value, account.currency);

  if (points.length < 2) {
    return h(
      "div",
      { class: "panel" },
      empty(
        "Not enough history to draw a line yet.",
        "The account adds one point per trading day and one per execution. Leave the platform running and the curve builds itself.",
      ),
    );
  }
  return h(
    "div",
    { class: "panel" },
    h(
      "div",
      { class: "panel-pad" },
      h("p", {
        class: "muted",
        text: useDaily
          ? `Daily account value in ${account.currency}.`
          : `Account value in ${account.currency} after each execution. A daily line takes over once two daily snapshots exist.`,
      }),
    ),
    lineChart(points, {
      format,
      tickFormat: (value, digits) => fmtNumber(value, { digits }),
      ariaLabel: `Account equity in ${account.currency}`,
    }),
    asTable(
      [
        { label: useDaily ? "Day" : "Execution", cell: (row) => row.label },
        { label: "Equity", numeric: true, cell: (row) => format(row.value) },
      ],
      [...points].reverse(),
    ),
  );
}

function realizedPanel(account) {
  if (!account.realized) {
    return h("div", { class: "panel" }, empty("Realized profit and loss is not available right now.", "Try again in a moment."));
  }
  const rows = account.realized
    .map((row) => ({ label: `${row.strategy_id} · ${row.symbol}`, value: toNumber(row.value), as_of: row.as_of }))
    .filter((row) => row.value !== null)
    .sort((a, b) => b.value - a.value);
  if (!rows.length) {
    return h(
      "div",
      { class: "panel" },
      empty("No closed trades yet.", "Realized profit and loss appears when a position is closed."),
    );
  }
  const format = (value) => fmtMoney(value, account.currency, { sign: true });
  return h(
    "div",
    { class: "panel" },
    h("div", { class: "panel-pad" }, h("p", { class: "muted", text: `Closed trades only, in ${account.currency}, as recorded by the execution engine.` })),
    divergingBars(rows, { format, ariaLabel: "Realized profit and loss by strategy and symbol" }),
    h(
      "div",
      { class: "legend" },
      h("span", {}, h("i", { class: "key-gain" }), "Gain"),
      h("span", {}, h("i", { class: "key-loss" }), "Loss"),
    ),
    asTable(
      [
        { label: "Strategy and symbol", cell: (row) => row.label },
        { label: "Realized", numeric: true, cell: (row) => format(row.value) },
        { label: "Recorded", cell: (row) => fmtDateTime(row.as_of) },
      ],
      rows,
    ),
  );
}

function summaryStrip(account) {
  const equity = account.equity ? toNumber(account.equity.value) : null;
  const start = toNumber(account.starting_equity);
  const change = equity !== null && start ? equity - start : null;
  // Summed on the server in exact decimals; never re-added here in floating point.
  const realizedTotal = account.realized_total;
  const fees = account.fees;
  return h(
    "div",
    { class: "panel strip" },
    h(
      "div",
      { class: "tile tile-hero" },
      h("span", { class: "tile-label", text: "Account equity" }),
      h("span", { class: "tile-value" }, equity === null ? "Not available" : [fmtNumber(equity), h("small", { text: account.currency })]),
      change === null
        ? null
        : h(
            "div",
            { class: "tile-delta" },
            delta(change, account.currency),
            h("span", { class: "muted", text: `${fmtPercent(change / start)} since start` }),
          ),
    ),
    h(
      "div",
      { class: "tile" },
      h("span", { class: "tile-label", text: "Realized profit and loss" }),
      h(
        "span",
        { class: "tile-value" },
        realizedTotal == null ? "Not available" : delta(realizedTotal, account.currency),
      ),
      h("span", { class: "tile-note", text: "Closed trades only." }),
    ),
    h(
      "div",
      { class: "tile" },
      h("span", { class: "tile-label", text: "Fees paid" }),
      h(
        "span",
        { class: "tile-value" },
        fees == null
          ? "Not available"
          : fees.length
            ? fees.map((fee) => h("div", {}, fmtNumber(fee.value), h("small", { text: fee.currency })))
            : "0",
      ),
      h("span", {
        class: "tile-note",
        text: fees == null ? "Try again in a moment." : "In the currency each fee was charged.",
      }),
    ),
    h(
      "div",
      { class: "tile" },
      h("span", { class: "tile-label", text: "Open positions" }),
      h("span", { class: "tile-value", text: account.positions == null ? "Not available" : fmtNumber(account.positions.length, { digits: 0 }) }),
    ),
  );
}

function positionsPanel(account) {
  if (!account.positions) {
    return h("div", { class: "panel" }, empty("Positions are not available right now.", "Try again in a moment."));
  }
  if (!account.positions.length) {
    return h("div", { class: "panel" }, empty("Nothing is held right now.", "Open positions appear here while a trade is running."));
  }
  return h(
    "div",
    { class: "panel" },
    table(
      [
        { label: "Symbol", cell: (row) => h("span", { class: "cell-main", text: row.symbol }) },
        { label: "Quantity", numeric: true, cell: (row) => fmtQuantity(row.quantity) },
        { label: "Average price", numeric: true, cell: (row) => fmtNumber(row.average_price) },
        { label: "Last mark", numeric: true, cell: (row) => fmtNumber(row.last_mark) },
        { label: "Value", numeric: true, cell: (row) => fmtMoney(row.notional, row.notional_currency) },
        { label: "Recorded", cell: (row) => fmtDateTime(row.as_of) },
      ],
      account.positions,
    ),
  );
}

function rangeFilter(context) {
  return h(
    "div",
    { class: "topbar-tools", role: "group", "aria-label": "History window" },
    RANGES.map((days) =>
      h("button", {
        class: "tool",
        type: "button",
        "aria-pressed": String(local.days === days),
        text: `${days} days`,
        onclick: () => {
          local.days = days;
          context.refresh();
        },
      }),
    ),
  );
}

function blotter(fills, context) {
  if (!fills.rows.length) {
    return h("div", { class: "panel" }, empty("No trades yet.", "Every filled order is listed here, newest first."));
  }
  const capped = !fills.more && fills.rows.length >= MAX_ROWS;
  const footer = fills.more
    ? h(
        "div",
        { class: "more" },
        h("button", {
          class: "tool",
          type: "button",
          text: "Load older trades",
          onclick: () => loadOlder(context),
        }),
      )
    : capped
      ? h("div", { class: "more muted", text: `Showing the newest ${fmtNumber(MAX_ROWS, { digits: 0 })} trades.` })
      : null;
  return h(
    "div",
    { class: "panel" },
    table(
      [
        { label: "When", cell: (row) => fmtDateTime(row.at) },
        { label: "Symbol", cell: (row) => h("span", { class: "cell-main", text: row.symbol }) },
        { label: "Side", cell: (row) => h("span", { class: "side", text: titleCase(row.side) }) },
        { label: "Quantity", numeric: true, cell: (row) => fmtQuantity(row.quantity) },
        { label: "Price", numeric: true, cell: (row) => fmtMoney(row.price, row.currency) },
        { label: "Fee", numeric: true, cell: (row) => fmtMoney(row.fee, row.fee_currency) },
        { label: "Strategy", cell: (row) => row.strategy_id },
      ],
      fills.rows,
    ),
    footer,
  );
}

export function view(data, context) {
  const { pnl, fills } = data;
  if (!(pnl.accounts || []).length) {
    return [
      section(
        "Profit and loss",
        null,
        h("div", { class: "panel" }, empty("No broker account is linked yet.", "Link a paper account during first-run setup and its results appear here.")),
      ),
    ];
  }
  const nodes = [section("History window", "Applies to the equity curve.", rangeFilter(context))];
  for (const account of pnl.accounts) {
    nodes.push(
      section(account.name, `All figures in ${account.currency}.`, summaryStrip(account)),
      section("How the account moved", null, h("div", { class: "grid-2" }, equityPanel(account), realizedPanel(account))),
      section("Open positions", null, positionsPanel(account)),
    );
  }
  const loaded = `${fmtNumber(fills.rows.length, { digits: 0 })} loaded, newest first.`;
  nodes.push(section("Trades", fills.rows.length ? loaded : null, blotter(fills, context)));
  return nodes;
}
