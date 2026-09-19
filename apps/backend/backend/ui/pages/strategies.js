// Strategies: what exists in the catalogue, what is bound to an account, what it did last.

import { delta, empty, fmtAgo, fmtDateTime, fmtNumber, h, section, table, titleCase } from "../format.js";

export function load(api) {
  return api.get("strategies");
}

// A strategy trades only once maintenance releases it; until then the catalogue
// row is fail-closed and the admin tools refuse to bind it.
function statusChip(row) {
  if (!row.version) return h("span", { class: "chip chip-off", text: "No version" });
  if (row.released) {
    return h("span", { class: "chip chip-on", text: titleCase(row.status || "released") });
  }
  return h("span", { class: "chip chip-off", text: "Not released" });
}

function bindingCell(row) {
  if (!(row.bindings || []).length) {
    return h("span", {
      class: "muted",
      text: row.released
        ? "Not bound to an account"
        : "Cannot be bound until it is released for trading",
    });
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
  const trading = rows.filter((row) => (row.bindings || []).some((b) => b.active)).length;
  const bound = rows.filter((row) => (row.bindings || []).length).length;
  const released = rows.filter((row) => row.released).length;

  const summary = h(
    "div",
    { class: "panel strip" },
    ...[
      ["In the catalogue", rows.length, "Registered in this installation's database."],
      ["Released for trading", released, "Approved by maintenance; the rest are read-only."],
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
            cell: (row) => [row.version ? `${row.version} ` : null, statusChip(row)],
          },
          { label: "Account binding", wrap: true, cell: bindingCell },
          { label: "Last signal", cell: signalCell },
          { label: "Realized P&L", numeric: true, cell: pnlCell },
        ],
        rows,
      )
    : empty(
        "No strategy is registered yet.",
        "Strategies ship with the installation but are listed here only after they are registered in the database.",
      );

  return [
    section("At a glance", null, summary),
    section(
      "All strategies",
      "A strategy trades only once it is released for trading, bound to an account and switched on. Releasing and binding are done with the admin tools, never from this page.",
      h("div", { class: "panel" }, list),
    ),
  ];
}
