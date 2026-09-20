// Settings: the owner's own profile, their broker accounts, and what has been
// changed lately. Everything configuration rather than activity lives here.
//
// No field on this page accepts key material. Broker credentials are added and
// rotated with `vmdev user account --secrets-file`; the account card shows only
// whether one exists, its status and its expiry.

import { confirmAction } from "../app.js";
import {
  choice,
  empty,
  field,
  fieldError,
  fmtAgo,
  fmtDateTime,
  h,
  section,
  table,
  textField,
  titleCase,
} from "../format.js";

const ACCOUNT_STATUSES = ["connected", "revoked", "error"];
const PROFILE_FIELDS = [
  { key: "full_name", label: "Display name", hint: "Shown on this dashboard only." },
  { key: "tz", label: "Timezone", hint: "An IANA name, for example Europe/Amsterdam." },
];

export function load(api) {
  return api.get("control");
}

// One edit: read the current value, send it back as `expected` beside the
// change, and let the server refuse if it moved underneath us.
function editableField(id, label, current, hint, save, { disabled, disabledHint } = {}) {
  const error = fieldError("");
  let draft = current === null || current === undefined ? "" : String(current);
  const input = textField(id, draft, { onInput: (value) => { draft = value; } });
  if (disabled) input.readOnly = true;

  const apply = h(
    "button",
    {
      class: "tool",
      type: "button",
      disabled: Boolean(disabled),
      onclick: async () => {
        const next = draft.trim();
        if (next === String(current ?? "").trim()) {
          error.textContent = "That is already the value.";
          return;
        }
        error.textContent = "";
        const done = await confirmAction({
          title: label,
          detail: `From "${current ?? ""}" to "${next}".`,
          confirmLabel: "Save",
          run: () => save(next),
        });
        if (done) input.blur();
      },
    },
    "Save",
  );

  return h(
    "div",
    { class: "field-row" },
    field(id, label, input, disabled ? disabledHint || hint : hint),
    h("div", { class: "editor-actions" }, apply, error),
  );
}

function profilePanel(owner, ctx) {
  if (!owner) {
    return empty(
      "Your profile is not available.",
      "The backend could not read it. Check that a deployment owner is designated.",
    );
  }
  const hasAccounts = (ctx.accounts || []).length > 0;
  return h(
    "div",
    { class: "panel" },
    h(
      "dl",
      { class: "facts" },
      h("dt", { text: "Owner id" }),
      h("dd", { text: owner.user_id }),
      h("dt", { text: "Email" }),
      h("dd", { text: owner.email }),
      h("dt", { text: "Status" }),
      h("dd", { text: titleCase(owner.status || "") }),
    ),
    ...PROFILE_FIELDS.map((item) =>
      editableField(
        `owner-${item.key}`,
        item.label,
        owner[item.key],
        item.hint,
        (value) =>
          ctx.api.send("PATCH", "/owner", {
            expected: { [item.key]: owner[item.key] },
            changes: { [item.key]: value },
          }),
      ),
    ),
    editableField(
      "owner-base_ccy",
      "Accounting currency",
      owner.base_ccy,
      "Three to ten uppercase letters, for example EUR.",
      (value) =>
        ctx.api.send("PATCH", "/owner", {
          expected: { base_ccy: owner.base_ccy },
          changes: { base_ccy: value.toUpperCase() },
        }),
      {
        // The service refuses this once any account exists, so the field is
        // read-only rather than a control that always fails.
        disabled: hasAccounts,
        disabledHint:
          "Fixed once a broker account exists, because recorded figures are " +
          "already denominated in it.",
      },
    ),
  );
}

function credentialLine(account) {
  const credential = account.credential || {};
  if (account.broker_code === "paper") {
    return h("span", { class: "muted", text: "Local simulator — no credential needed" });
  }
  if (!credential.present) {
    return h("span", {
      class: "muted",
      text: "No credential. Add one with vmdev user account --secrets-file.",
    });
  }
  return h(
    "span",
    {},
    h("span", {
      class: credential.status === "active" ? "chip chip-on" : "chip chip-off",
      text: titleCase(credential.status || "unknown"),
    }),
    credential.expires_at
      ? h("span", { class: "cell-sub", text: `expires ${fmtDateTime(credential.expires_at)}` })
      : null,
  );
}

function accountCard(account, ctx) {
  const frozen =
    "Frozen once the account has execution activity, so recorded trades keep " +
    "the terms they were made under.";
  const statusError = fieldError("");
  let status = account.status;

  const statusControl = choice(
    `account-status-${account.account_id}`,
    ACCOUNT_STATUSES.map((value) => ({ value, label: titleCase(value) })),
    account.status,
    (value) => {
      status = value;
    },
  );

  const applyStatus = h(
    "button",
    {
      class: "tool",
      type: "button",
      onclick: async () => {
        if (status === account.status) {
          statusError.textContent = "That is already the status.";
          return;
        }
        statusError.textContent = "";
        const done = await confirmAction({
          title: `${account.display_name}: ${titleCase(status)}`,
          detail: `From ${titleCase(account.status)} to ${titleCase(status)}.`,
          consequence:
            status === "connected"
              ? "Strategies may be bound to this account again."
              : "Strategies cannot be bound to this account while it is not connected.",
          confirmLabel: "Save",
          run: () =>
            ctx.api.send("PATCH", `/broker-accounts/${account.account_id}`, {
              expected: { status: account.status },
              changes: { status },
            }),
        });
        if (done) ctx.refresh();
      },
    },
    "Save",
  );

  return h(
    "div",
    { class: "panel" },
    h("h3", { text: account.display_name || `Account ${account.account_id}` }),
    h(
      "dl",
      { class: "facts" },
      h("dt", { text: "Broker" }),
      h("dd", { text: `${account.broker_name} (${account.environment})` }),
      h("dt", { text: "Currency" }),
      h("dd", { text: account.base_ccy }),
      h("dt", { text: "Key" }),
      h("dd", { text: account.config_key || "—" }),
      h("dt", { text: "Credential" }),
      h("dd", {}, credentialLine(account)),
      account.paper_initial_equity ? h("dt", { text: "Starting equity" }) : null,
      account.paper_initial_equity
        ? h("dd", { text: `${account.paper_initial_equity} ${account.base_ccy}`, title: frozen })
        : null,
    ),
    editableField(
      `account-name-${account.account_id}`,
      "Display name",
      account.display_name,
      "What this account is called on this dashboard.",
      (value) =>
        ctx.api.send("PATCH", `/broker-accounts/${account.account_id}`, {
          expected: { display_name: account.display_name },
          changes: { display_name: value },
        }),
    ),
    h(
      "div",
      { class: "field-row" },
      field(
        `account-status-${account.account_id}`,
        "Status",
        statusControl,
        "Only a connected account can have strategies bound to it.",
      ),
      h("div", { class: "editor-actions" }, applyStatus, statusError),
    ),
  );
}

function activityPanel(activity) {
  if (!activity || !activity.length) {
    return empty("Nothing has been changed yet.", "Changes you make here will be listed.");
  }
  return h(
    "div",
    { class: "panel" },
    table(
      [
        {
          label: "When",
          cell: (row) => h("span", { title: fmtDateTime(row.at), text: fmtAgo(row.at) }),
        },
        { label: "Change", cell: (row) => row.action },
        {
          label: "Fields",
          wrap: true,
          cell: (row) => (row.fields || []).join(", ") || "—",
        },
        {
          label: "Result",
          cell: (row) =>
            h("span", {
              class: row.status === "ok" ? "chip chip-on" : "chip chip-off",
              text: row.status === "ok" ? "Applied" : "Refused",
            }),
        },
      ],
      activity,
    ),
  );
}

export function view(data, ctx) {
  const accounts = data.accounts || [];
  const context = { ...ctx, accounts };

  return [
    section(
      "You",
      "The single deployment owner. Everything on this installation belongs to this profile.",
      profilePanel(data.owner, context),
    ),
    section(
      "Broker accounts",
      "Credentials are never entered here. Add or rotate one with vmdev user account --secrets-file.",
      accounts.length
        ? h("div", {}, ...accounts.map((account) => accountCard(account, context)))
        : empty(
            "No broker account yet.",
            "vmdev deploy creates a local paper account on a fresh install; vmdev user account adds another.",
          ),
    ),
    section(
      "Recent changes",
      "Every change made through this dashboard or the admin tools, including the ones that were refused.",
      activityPanel(data.activity),
    ),
  ];
}
