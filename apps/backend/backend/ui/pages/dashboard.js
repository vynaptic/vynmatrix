// Dashboard: how the account stands, whether data is flowing, and what just happened.

import { sparkline } from "../charts.js";
import {
  ageSeconds,
  delta,
  empty,
  fmtAgo,
  fmtDateTime,
  fmtMoney,
  fmtNumber,
  fmtPercent,
  fmtQuantity,
  h,
  section,
  titleCase,
  toNumber,
} from "../format.js";

const FRESH_SECONDS = 10 * 60;
const DELAYED_SECONDS = 60 * 60;

export function load(api) {
  return api.get("overview");
}

function tile(label, value, note, extra, hero = false) {
  return h(
    "div",
    { class: hero ? "tile tile-hero" : "tile" },
    h("span", { class: "tile-label", text: label }),
    h("span", { class: "tile-value" }, value),
    extra || null,
    note ? h("span", { class: "tile-note", text: note }) : null,
  );
}

const SOURCES = {
  execution_snapshot: "recorded at the last execution",
  daily_nav: "from the daily account snapshot",
  starting_equity: "starting equity; nothing has traded yet",
};

function accountSection(account) {
  const equity = account.equity;
  const equityValue = equity ? toNumber(equity.value) : null;
  const start = toNumber(account.starting_equity);
  const change = equityValue !== null && start ? equityValue - start : null;
  const trend = (account.equity_trend || []).map(toNumber).filter((value) => value !== null);

  const equityTile = tile(
    "Account equity",
    equity
      ? [fmtNumber(equity.value), h("small", { text: account.currency })]
      : "Not available",
    equity
      ? `${titleCase(SOURCES[equity.source] || equity.source)}${equity.as_of ? `, ${fmtAgo(equity.as_of)}` : ""}`
      : "The execution engine has not recorded this account yet.",
    h(
      "div",
      {},
      h(
        "div",
        { class: "tile-delta" },
        change === null ? null : delta(change, account.currency),
        change === null || !start
          ? null
          : h("span", { class: "muted", text: `${fmtPercent(change / start)} since start` }),
      ),
      trend.length > 1 ? sparkline(trend, "Equity after each recent execution") : null,
    ),
    true,
  );

  const realized = account.realized_pnl;
  return section(
    account.name,
    `${titleCase(account.environment)} account · ${account.currency} · ${titleCase(account.status)}`,
    h(
      "div",
      { class: "panel strip" },
      equityTile,
      tile(
        "Realized profit and loss",
        realized ? delta(realized.value, account.currency) : "Not available",
        realized && realized.as_of
          ? `Closed trades only, recorded ${fmtAgo(realized.as_of)}`
          : "Appears after the first closed trade.",
      ),
      tile(
        "Open positions",
        account.open_positions === null ? "Not available" : fmtNumber(account.open_positions, { digits: 0 }),
        account.open_positions === 0 ? "Nothing is held right now." : null,
      ),
      tile(
        "Trades filled",
        account.fills === null ? "Not available" : fmtNumber(account.fills, { digits: 0 }),
        "All time, this account.",
      ),
    ),
  );
}

function marketStatus(iso) {
  const age = ageSeconds(iso);
  if (age === null) {
    return ["status", "No prices yet", "Prices arrive once the market-data worker is running."];
  }
  if (age <= FRESH_SECONDS) return ["status status-good", "Fresh", `Last price ${fmtAgo(iso)}.`];
  if (age <= DELAYED_SECONDS) {
    return ["status status-warning", "Delayed", `Last price ${fmtAgo(iso)}.`];
  }
  return [
    "status status-critical",
    "Stale",
    `No new prices since ${fmtDateTime(iso)}. The market-data worker may be stopped.`,
  ];
}

function pulseSection(data) {
  const [statusClass, statusText, statusNote] = marketStatus(data.freshness.last_price_at);
  const counts = data.strategies;
  return section(
    "Is everything running?",
    "Quiet strategies are normal; only market data is graded.",
    h(
      "div",
      { class: "panel strip" },
      tile("Market data", h("span", { class: statusClass, text: statusText }), statusNote),
      tile(
        "Last signal",
        fmtAgo(data.freshness.last_signal_at),
        data.freshness.last_signal_at
          ? fmtDateTime(data.freshness.last_signal_at)
          : "Strategies have not signalled yet.",
      ),
      tile(
        "Last trade",
        fmtAgo(data.freshness.last_fill_at),
        data.freshness.last_fill_at
          ? fmtDateTime(data.freshness.last_fill_at)
          : "No order has been filled yet.",
      ),
      tile(
        "Strategies trading",
        counts ? `${counts.active} of ${counts.catalogue}` : "Not available",
        counts ? `${counts.bound} bound to an account.` : null,
      ),
    ),
  );
}

function activityItem(item) {
  if (item.kind === "fill") {
    return h(
      "li",
      {},
      h("span", { class: "feed-kind", text: "Trade" }),
      h(
        "span",
        {},
        h("span", { class: "cell-main", text: `${titleCase(item.side)} ${fmtQuantity(item.quantity)} ${item.symbol}` }),
        h("span", { class: "cell-sub", text: `at ${fmtMoney(item.price, item.currency)} · ${item.strategy_id}` }),
      ),
      h("span", { class: "feed-when", title: fmtDateTime(item.at), text: fmtAgo(item.at) }),
    );
  }
  const confidence = toNumber(item.confidence);
  return h(
    "li",
    {},
    h("span", { class: "feed-kind", text: "Signal" }),
    h(
      "span",
      {},
      h("span", { class: "cell-main", text: `${titleCase(item.action)} ${item.symbol}` }),
      h("span", {
        class: "cell-sub",
        text: `${item.strategy_id}${confidence === null ? "" : ` · confidence ${fmtNumber(confidence)}`}`,
      }),
    ),
    h("span", { class: "feed-when", title: fmtDateTime(item.at), text: fmtAgo(item.at) }),
  );
}

export function view(data) {
  const accounts = data.accounts.length
    ? data.accounts.map(accountSection)
    : [
        section(
          "Your account",
          null,
          h(
            "div",
            { class: "panel" },
            empty("No broker account is linked yet.", "Link a paper account during first-run setup and it appears here."),
          ),
        ),
      ];
  const feed = data.activity.length
    ? h("ul", { class: "feed" }, data.activity.map(activityItem))
    : empty("Nothing has happened yet.", "Signals and trades appear here as soon as a strategy acts.");
  return [
    ...accounts,
    pulseSection(data),
    section("Recent activity", "Latest signals and trades, newest first.", h("div", { class: "panel" }, feed)),
  ];
}
