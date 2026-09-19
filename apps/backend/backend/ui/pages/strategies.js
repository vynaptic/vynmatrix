// Strategies: what exists in the catalogue, what is bound to an account, what it did last.

import { delta, empty, fmtAgo, fmtDateTime, fmtNumber, h, section, table, titleCase } from "../format.js";

export function load(api) {
  return api.get("strategies");
}

function statusChip(status) {
  if (!status) return h("span", { class: "chip chip-off", text: "No version" });
  return h("span", { class: status === "active" ? "chip chip-on" : "chip", text: titleCase(status) });
}

function bindingCell(row) {
  if (!(row.bindings || []).length) {
    return h("span", { class: "muted", text: "Not bound to an account" });
  }
  return row.bindings.map((binding) =>
    h(
      "div",
      {},
      h("span", { class: "cell-main", text: binding.account_name || `Account ${binding.account_id}` }),
      h(
        "span",
        { class: "cell-sub" },
        h("span", { class: binding.active ? "chip chip-on" : "chip chip-off", text: binding.active ? "Trading on" : "Trading off" }),
        " ",
        h("span", { class: binding.entries_enabled ? "chip" : "chip chip-off", text: binding.entries_enabled ? "Entries" : "No entries" }),
        " ",
        h("span", { class: binding.exits_enabled ? "chip" : "chip chip-off", text: binding.exits_enabled ? "Exits" : "No exits" }),
        " ",
        h("span", { class: binding.autopilot ? "chip" : "chip chip-off", text: binding.autopilot ? "Autopilot" : "Manual" }),
      ),
    ),
  );
}

function signalCell(row) {
  if (!row.last_signal) return h("span", { class: "muted", text: "None yet" });
  return [
    h("span", { class: "cell-main", text: `${titleCase(row.last_signal.action)} ${row.last_signal.symbol}` }),
    h("span", { class: "cell-sub", title: fmtDateTime(row.last_signal.at), text: fmtAgo(row.last_signal.at) }),
  ];
}

function pnlCell(row) {
  if (!row.realized_pnl) return h("span", { class: "muted", text: "Not available" });
  if (!row.realized_pnl.length) return h("span", { class: "muted", text: "No closed trades" });
  return row.realized_pnl.map((entry) => h("div", {}, delta(entry.value, entry.currency)));
}

export function view(data) {
  const rows = data.strategies || [];
  const trading = rows.filter((row) => row.bindings.some((binding) => binding.active)).length;
  const bound = rows.filter((row) => row.bindings.length).length;

  const summary = h(
    "div",
    { class: "panel strip" },
    ...[
      ["In the catalogue", rows.length, "Strategies shipped with this installation."],
      ["Bound to an account", bound, "Allowed to trade a specific account."],
      ["Trading now", trading, "Bound and switched on."],
    ].map(([label, value, note]) =>
      h(
        "div",
        { class: "tile" },
        h("span", { class: "tile-label", text: label }),
        h("span", { class: "tile-value", text: fmtNumber(value, { digits: 0 }) }),
        h("span", { class: "tile-note", text: note }),
      ),
    ),
  );

  const list = rows.length
    ? table(
        [
          {
            label: "Strategy",
            wrap: true,
            cell: (row) => [
              h("span", { class: "cell-main", text: row.name }),
              h("span", { class: "cell-sub", text: row.description || row.strategy_id }),
            ],
          },
          { label: "Market", cell: (row) => titleCase(row.asset_class) },
          {
            label: "Version",
            cell: (row) => [row.version ? `${row.version} ` : null, statusChip(row.status)],
          },
          { label: "Account binding", wrap: true, cell: bindingCell },
          { label: "Last signal", cell: signalCell },
          { label: "Realized P&L", numeric: true, cell: pnlCell },
        ],
        rows,
      )
    : empty("The catalogue is empty.", "Strategies are registered during first-run setup.");

  return [
    section("At a glance", null, summary),
    section(
      "All strategies",
      "A strategy trades only while it is bound to an account and switched on. Bindings are changed with the admin tools, never from this page.",
      h("div", { class: "panel" }, list),
    ),
  ];
}
