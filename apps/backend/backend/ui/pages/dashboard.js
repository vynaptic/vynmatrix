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

// A feed is judged against its own cadence: a one-minute feed an hour behind is
// stale, a daily reference rate an hour old is perfectly fresh.
const UNIT_SECONDS = { m: 60, h: 3600, d: 86400, w: 604800 };
const FLOOR_FRESH = 10 * 60;
const FLOOR_DELAYED = 60 * 60;
const LEVELS = ["good", "warning", "critical"];

function cadenceSeconds(timeframe) {
  const match = /^(\d+)\s*([mhdw])/i.exec(String(timeframe || ""));
  return match ? Number(match[1]) * UNIT_SECONDS[match[2].toLowerCase()] : 60;
}

function feedLevel(feed) {
  const age = ageSeconds(feed.last_at);
  const cadence = cadenceSeconds(feed.timeframe);
  if (age === null) return 2;
  if (age <= Math.max(FLOOR_FRESH, cadence * 2)) return 0;
  return age <= Math.max(FLOOR_DELAYED, cadence * 6) ? 1 : 2;
}

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
        account.open_positions == null
          ? "Not available"
          : fmtNumber(account.open_positions, { digits: 0 }),
        account.open_positions === 0 ? "Nothing is held right now." : null,
      ),
      tile(
        "Trades filled",
        account.fills == null ? "Not available" : fmtNumber(account.fills, { digits: 0 }),
        "All time, this account.",
      ),
    ),
  );
}

function marketStatus(feeds) {
  if (!feeds) {
    return ["status", "Not available", "Price freshness could not be read just now."];
  }
  if (!feeds.length) {
    return [
      "status status-critical",
      "No recent prices",
      "Nothing stored in the last two weeks. Prices arrive once the market-data worker is running.",
    ];
  }
  const worst = Math.max(...feeds.map(feedLevel));
  const detail = [...feeds]
    .sort((a, b) => cadenceSeconds(a.timeframe) - cadenceSeconds(b.timeframe))
    .map((feed) => `${feed.timeframe} prices ${fmtAgo(feed.last_at)}`)
    .join(" \u00B7 ");
  const label = ["Fresh", "Delayed", "Behind"][worst];
  const hint = worst === 2 ? " A feed this far behind is still catching up or has stopped." : "";
  return [`status status-${LEVELS[worst]}`, label, `${detail}.${hint}`];
}

function pulseSection(data) {
  const freshness = data.freshness || {};
  const [statusClass, statusText, statusNote] = marketStatus(freshness.price_feeds);
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
        fmtAgo(freshness.last_signal_at),
        freshness.last_signal_at
          ? fmtDateTime(freshness.last_signal_at)
          : "Strategies have not signalled yet.",
      ),
      tile(
        "Last trade",
        fmtAgo(freshness.last_fill_at),
        freshness.last_fill_at
          ? fmtDateTime(freshness.last_fill_at)
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
  const activity = data.activity || [];
  const accounts = (data.accounts || []).length
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
  const feed = activity.length
    ? h("ul", { class: "feed" }, activity.map(activityItem))
    : empty("Nothing has happened yet.", "Signals and trades appear here as soon as a strategy acts.");
  return [
    ...accounts,
    pulseSection(data),
    section("Recent activity", "Latest signals and trades, newest first.", h("div", { class: "panel" }, feed)),
  ];
}
