// Strategies: what exists in the catalogue, what is bound to an account, what
// it did last -- and the control that switches a binding on or off and sets
// which instruments it trades.

import { confirmAction } from "../app.js";
import {
  choice,
  delta,
  empty,
  field,
  fieldError,
  fmtAgo,
  fmtDateTime,
  fmtNumber,
  h,
  section,
  table,
  textField,
  titleCase,
} from "../format.js";

// The four authority flags are constrained by three CHECKs, so most of their
// sixteen combinations are illegal. These three are the legal subset, and the
// server maps each to the same flags -- a mode cannot build a rejected row.
const MODES = [
  { value: "off", label: "Off", consequence: "It will not trade. Its history is kept." },
  {
    value: "close_only",
    label: "Close only",
    consequence: "It may close positions it already holds. It will not open anything new.",
  },
  {
    value: "trading",
    label: "Trading",
    consequence: "It will open and close positions on its own.",
  },
];
const MODE_LABEL = Object.fromEntries(MODES.map((mode) => [mode.value, mode.label]));

export function load(api) {
  return Promise.all([api.get("strategies"), api.get("control")]).then(([strategies, control]) => ({
    ...strategies,
    accounts: control.accounts || [],
  }));
}

// A strategy trades only once maintenance releases it. `released` is the same
// condition the server's bind gate applies, so a control is never offered for a
// strategy the API would refuse.
function statusChip(row) {
  if (!row.version) return h("span", { class: "chip chip-off", text: "No version" });
  if (row.released) {
    return h("span", { class: "chip chip-on", text: titleCase(row.status || "released") });
  }
  return h("span", { class: "chip chip-off", text: "Not released" });
}

function accountLabel(accounts, accountId) {
  const found = accounts.find((item) => item.account_id === accountId);
  return found ? found.display_name || `account ${accountId}` : `account ${accountId}`;
}

function modeChip(mode) {
  if (mode === "trading") return h("span", { class: "chip chip-on", text: "Trading" });
  if (mode === "close_only") return h("span", { class: "chip", text: "Close only" });
  if (mode === "custom") return h("span", { class: "chip", text: "Custom" });
  return h("span", { class: "chip chip-off", text: "Off" });
}

// One status per readiness check. The marker's shape, not only its colour,
// carries the state (see .status-* in app.css).
const CHECK_STATUS = {
  ok: "status status-good",
  warn: "status status-warning",
  blocked: "status status-critical",
  unknown: "status",
};

// Why a binding is or is not trading, as the server recorded it. A blocked
// check is a condition that stops it; a warning reports what last happened.
function readiness(binding) {
  const checklist = binding.readiness;
  if (!checklist) {
    return h("span", { class: "cell-sub", text: "Checklist unavailable" });
  }
  const blocked = checklist.checks.filter((check) => check.state === "blocked");
  const summary = checklist.ready
    ? h("span", { class: "status status-good", text: "Ready" })
    : h("span", {
        class: "status status-critical",
        text: `Blocked: ${blocked.map((check) => check.label.toLowerCase()).join(", ")}`,
      });
  return h(
    "details",
    { class: "readiness" },
    h("summary", {}, summary),
    h(
      "ul",
      { class: "checklist" },
      checklist.checks.map((check) =>
        h(
          "li",
          {},
          h("span", { class: CHECK_STATUS[check.state] || "status", text: check.label }),
          h("span", { class: "check-detail", text: check.detail }),
          check.at
            ? h("span", { class: "cell-sub", title: fmtDateTime(check.at), text: fmtAgo(check.at) })
            : null,
        ),
      ),
    ),
  );
}

// A binding's scope is its strategy's declared universe. Without a list it is
// every instrument in the binding's asset classes, none of which another
// active strategy on that account may trade.
function scopeLabel(binding) {
  const instruments = binding.instruments_allowed || [];
  const classes = binding.asset_classes_allowed || [];
  if (instruments.length) return `Trades ${instruments.join(", ")}`;
  return classes.length
    ? `Trades every ${classes.join(" or ")} instrument`
    : "Trades every instrument";
}

// The scope the owner typed, as a list of symbols. The server resolves each one
// through the instrument catalogue and refuses one it does not know.
function parseScope(text) {
  return [...new Set(text.split(/[\s,]+/).filter(Boolean))];
}

// Whether two scopes name the same instruments, ignoring order and case. Only
// the server knows broker aliases, so an alias of a current symbol is sent and
// resolves to the scope the binding already has.
function sameScope(left, right) {
  const a = new Set((left || []).map((item) => item.toUpperCase()));
  const b = new Set((right || []).map((item) => item.toUpperCase()));
  return a.size === b.size && [...a].every((item) => b.has(item));
}

function bindingCell(row) {
  if (!(row.bindings || []).length) {
    return h("span", {
      class: "muted",
      text: row.released
        ? "Not bound to an account"
        : "Cannot be switched on until it is released for trading",
    });
  }
  return row.bindings.map((binding) =>
    h(
      "div",
      {},
      h("span", {
        class: "cell-main",
        text: binding.account_name || `Account ${binding.account_id}`,
      }),
      h("span", { class: "cell-sub" }, modeChip(binding.mode)),
      h("span", { class: "cell-sub", text: scopeLabel(binding) }),
      readiness(binding),
    ),
  );
}

function signalCell(row) {
  if (!row.last_signal) return h("span", { class: "muted", text: "None yet" });
  return [
    h("span", {
      class: "cell-main",
      text: `${titleCase(row.last_signal.action)} ${row.last_signal.symbol}`,
    }),
    h("span", { class: "cell-sub", title: fmtDateTime(row.last_signal.at), text: fmtAgo(row.last_signal.at) }),
  ];
}

function pnlCell(row) {
  if (!row.realized_pnl) return h("span", { class: "muted", text: "Not available" });
  if (!row.realized_pnl.length) return h("span", { class: "muted", text: "No closed trades" });
  return row.realized_pnl.map((entry) => h("div", {}, delta(entry.value, entry.currency)));
}

// The editor for one strategy. It writes through a confirmation, and a refusal
// lands beside the control rather than in the page-wide banner.
function editor(row, ctx) {
  const connected = (ctx.accounts || []).filter((account) => account.status === "connected");
  const binding = (row.bindings || [])[0] || null;
  const error = fieldError("");

  if (!row.released) {
    return h("div", { class: "editor" }, h("p", {
      class: "muted",
      text:
        "This strategy is not released for trading. Releasing is a maintenance " +
        "action and is not done from this page.",
    }));
  }
  if (!connected.length) {
    return h("div", { class: "editor" }, h("p", {
      class: "muted",
      text:
        "No connected account to bind to. `vmdev deploy` creates a local paper " +
        "account on a fresh install; `vmdev user account` adds another.",
    }));
  }

  let account = binding ? binding.account_id : connected[0].account_id;
  let mode = binding ? binding.mode : "off";
  const currentScope = binding ? binding.instruments_allowed : null;
  let scopeDraft = currentScope ? currentScope.join(", ") : "";

  const accountControl = choice(
    `account-${row.strategy_id}`,
    connected.map((item) => ({
      value: String(item.account_id),
      label: item.display_name || `Account ${item.account_id}`,
    })),
    String(account),
    (value) => {
      account = Number(value);
    },
  );
  const modeControl = choice(
    `mode-${row.strategy_id}`,
    MODES.map((item) => ({ value: item.value, label: item.label })),
    mode,
    (value) => {
      mode = value;
    },
  );
  if (binding) accountControl.disabled = true;
  const scopeControl = binding
    ? textField(`scope-${row.strategy_id}`, scopeDraft, {
        onInput: (value) => {
          scopeDraft = value;
        },
      })
    : null;

  const apply = h("button", {
    class: "primary",
    type: "button",
    onclick: async () => {
      const chosen = MODES.find((item) => item.value === mode);
      const current = binding ? binding.mode : "off";
      const instruments = parseScope(scopeDraft);
      const modeChanged = !binding || mode !== current;
      const scopeChanged = Boolean(binding) && !sameScope(instruments, currentScope);
      if (!modeChanged && !scopeChanged) {
        error.textContent = "That is already its mode and its scope.";
        return;
      }
      if (scopeChanged && !instruments.length) {
        error.textContent =
          "Name at least one instrument. Widening a binding to every instrument " +
          "is not done from this page.";
        return;
      }
      error.textContent = "";
      // Only what changed is sent, each fenced by the value shown on this page.
      const expected = {};
      const changes = {};
      if (modeChanged) {
        expected.mode = current;
        changes.mode = mode;
      }
      if (scopeChanged) {
        expected.instruments_allowed = currentScope;
        changes.instruments_allowed = instruments;
      }
      const where = binding ? binding.account_name || `account ${binding.account_id}` : "";
      const done = await confirmAction({
        title: `${row.name}: ${modeChanged ? chosen.label : "Instrument scope"}`,
        detail: binding
          ? [
              modeChanged
                ? `On ${where}, from ${MODE_LABEL[current] || current} to ${chosen.label}.`
                : `On ${where}.`,
              scopeChanged
                ? `It trades ${instruments.join(", ")} instead of ` +
                  `${currentScope ? currentScope.join(", ") : "every instrument"}.`
                : null,
            ]
              .filter(Boolean)
              .join(" ")
          : `Bind to ${accountLabel(connected, account)} as ${chosen.label}, ` +
            "for the instruments and asset class its strategy declares.",
        consequence: [
          modeChanged ? chosen.consequence : null,
          scopeChanged
            ? "Signals for any other instrument no longer reach it. A scope that drops an " +
              "instrument it still holds, or shares one with another active strategy on " +
              "this account, is refused."
            : null,
        ]
          .filter(Boolean)
          .join(" "),
        confirmLabel: modeChanged ? chosen.label : "Change scope",
        run: async () => {
          if (binding) {
            await ctx.api.send("POST", `/api/ui/bindings/${binding.binding_id}`, {
              expected,
              changes,
            });
          } else {
            await ctx.api.send("POST", "/api/ui/bindings", {
              strategy_id: row.strategy_id,
              broker_account_id: account,
              mode,
            });
          }
        },
      });
      if (done) ctx.refresh();
    },
  }, binding ? "Apply" : "Bind");

  return h(
    "div",
    { class: "editor" },
    field(`account-${row.strategy_id}`, "Account", accountControl,
      binding ? "An existing binding stays on its account." : null),
    field(`mode-${row.strategy_id}`, "Mode", modeControl,
      "Entries always run on autopilot; close-only never opens a position."),
    scopeControl
      ? field(`scope-${row.strategy_id}`, "Instruments", scopeControl,
        "Catalogue symbols, separated by commas. Narrow it so it shares no " +
          "instrument with another active strategy on this account.")
      : null,
    h("div", { class: "editor-actions" }, apply, error),
  );
}

export function view(data, ctx) {
  const rows = data.strategies || [];
  const accounts = data.accounts || [];
  const trading = rows.filter((row) =>
    (row.bindings || []).some((b) => b.mode === "trading" || b.mode === "close_only"),
  ).length;
  const bound = rows.filter((row) => (row.bindings || []).length).length;
  const released = rows.filter((row) => row.released).length;

  const summary = h(
    "div",
    { class: "panel strip" },
    ...[
      ["In the catalogue", rows.length, "Registered in this installation's database."],
      ["Released for trading", released, "Approved by maintenance; the rest are read-only."],
      ["Bound to an account", bound, "Allowed to trade a specific account."],
      ["Trading now", trading, "Switched on, opening or at least closing."],
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
              h(
                "details",
                {},
                h("summary", { text: row.released ? "Change" : "Why not?" }),
                editor(row, { ...ctx, accounts }),
              ),
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
      "A strategy trades only once maintenance releases it, it is bound to an account, you switch it on, and a signal worker is running it. Open a binding's status to see which of these holds. Releasing is not done from this page; switching on is. Strategies can share an account when they trade no instrument in common.",
      h("div", { class: "panel" }, list),
    ),
  ];
}
