// Formatting and DOM helpers. Everything that reaches the page goes through
// textContent, so strategy names and other stored text are never parsed as markup.

const SVG_NS = "http://www.w3.org/2000/svg";
const DASH = "—";

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  applyAttrs(node, attrs);
  append(node, children);
  return node;
}

export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  applyAttrs(node, attrs);
  append(node, children);
  return node;
}

function applyAttrs(node, attrs) {
  for (const [name, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.setAttribute("class", value);
    else if (name === "text") node.textContent = String(value);
    else if (name.startsWith("on") && typeof value === "function") {
      node.addEventListener(name.slice(2), value);
    } else node.setAttribute(name, value === true ? "" : String(value));
  }
}

function append(node, children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

const TINY = 0.01;

function magnitudeText(magnitude, digits, allowTiny) {
  // A very small price or quantity keeps its significant digits instead of "0.00".
  if (allowTiny && magnitude !== 0 && magnitude < TINY && digits > 0) {
    return new Intl.NumberFormat(undefined, { maximumSignificantDigits: 4 }).format(magnitude);
  }
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: Math.min(2, digits),
    maximumFractionDigits: digits,
  }).format(magnitude);
}

// True when the value shows as zero at this precision; such a value carries no sign.
export function roundsToZero(value, digits = 2) {
  const number = toNumber(value);
  return number === null || Math.abs(number) < 0.5 * 10 ** -digits;
}

// money: a rounding residue is simply zero, never a signed or tiny-magnitude figure.
export function fmtNumber(value, { digits = 2, sign = false, money = false } = {}) {
  const number = toNumber(value);
  if (number === null) return DASH;
  const magnitude = money && roundsToZero(number, digits) ? 0 : Math.abs(number);
  const text = magnitudeText(magnitude, digits, !money);
  if (magnitude === 0) return text;
  if (number < 0) return `−${text}`;
  return sign ? `+${text}` : text;
}

// Currency codes here include non-ISO ones (USDC), so the code is a suffix
// rather than an Intl currency style, which rejects them.
export function fmtMoney(value, currency, options = {}) {
  const text = fmtNumber(value, { ...options, money: true });
  return text === DASH || !currency ? text : `${text} ${currency}`;
}

export function fmtQuantity(value) {
  return fmtNumber(value, { digits: 6 });
}

export function fmtPercent(ratio) {
  if (ratio === null || !Number.isFinite(ratio)) return DASH;
  const text = new Intl.NumberFormat(undefined, {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Math.abs(ratio));
  if (ratio < 0) return `−${text}`;
  return ratio > 0 ? `+${text}` : text;
}

export function fmtDateTime(iso) {
  if (!iso) return DASH;
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return DASH;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    value,
  );
}

export function fmtDate(iso) {
  if (!iso) return DASH;
  const value = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(value.getTime())) return DASH;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(value);
}

export function fmtClock(date) {
  return new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(date);
}

export function ageSeconds(iso) {
  if (!iso) return null;
  const value = new Date(iso).getTime();
  return Number.isNaN(value) ? null : Math.max(0, (Date.now() - value) / 1000);
}

export function fmtAgo(iso) {
  const seconds = ageSeconds(iso);
  if (seconds === null) return "never";
  if (seconds < 45) return "just now";
  const units = [
    [60, "minute"],
    [3600, "hour"],
    [86400, "day"],
    [2592000, "month"],
    [31536000, "year"],
  ];
  let label = "minute";
  let size = 60;
  for (const [unitSeconds, unit] of units) {
    if (seconds >= unitSeconds) {
      label = unit;
      size = unitSeconds;
    }
  }
  const count = Math.max(1, Math.round(seconds / size));
  return `${count} ${label}${count === 1 ? "" : "s"} ago`;
}

export function titleCase(text) {
  if (!text) return DASH;
  const spaced = String(text).replace(/[_-]+/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function delta(value, currency) {
  const number = toNumber(value);
  if (number === null) return h("span", { class: "delta delta-flat", text: "Not recorded yet" });
  const direction = roundsToZero(number) ? "flat" : number > 0 ? "up" : "down";
  const glyph = { up: "▲", down: "▼", flat: "●" }[direction];
  return h(
    "span",
    { class: `delta delta-${direction}` },
    h("span", { "aria-hidden": "true", text: glyph }),
    fmtMoney(number, currency, { sign: true }),
  );
}

export function section(title, note, ...children) {
  return h(
    "section",
    { class: "section" },
    h(
      "div",
      { class: "section-head" },
      h("h2", { text: title }),
      note ? h("p", { text: note }) : null,
    ),
    ...children,
  );
}

export function empty(headline, explanation) {
  return h("div", { class: "empty" }, h("strong", { text: headline }), explanation);
}

export function table(columns, rows) {
  const head = h(
    "tr",
    {},
    columns.map((column) =>
      h("th", { class: column.numeric ? "num" : null, scope: "col", text: column.label }),
    ),
  );
  const body = rows.map((row) =>
    h(
      "tr",
      {},
      columns.map((column) => {
        const classes = [column.numeric ? "num" : null, column.wrap ? "wrap" : null]
          .filter(Boolean)
          .join(" ");
        return h("td", { class: classes || null }, column.cell(row));
      }),
    ),
  );
  return h(
    "div",
    { class: "table-wrap" },
    h("table", {}, h("thead", {}, head), h("tbody", {}, body)),
  );
}

// ---------------------------------------------------------------- controls
// h() sets attributes, which cannot express live control state: an input's
// value, a select's selection and a checkbox's checked flag are properties, and
// writing the attribute does not move them once the node exists. These helpers
// set properties, so a re-render shows what the control actually holds.

export function control(tag, attrs = {}, props = {}, ...children) {
  const node = h(tag, attrs, ...children);
  for (const [name, value] of Object.entries(props)) node[name] = value;
  return node;
}

// A labelled control. The label is a real <label for>, so clicking it focuses
// the control and a screen reader announces the two together.
export function field(id, label, node, hint) {
  return h(
    "div",
    { class: "field" },
    h("label", { for: id, text: label }),
    node,
    hint ? h("p", { class: "field-hint", text: hint }) : null,
  );
}

export function choice(id, options, value, onChange) {
  const select = control(
    "select",
    { id, name: id, onchange: (event) => onChange(event.target.value) },
    {},
    ...options.map((option) =>
      control("option", { value: option.value }, { selected: option.value === value }, option.label),
    ),
  );
  // Selection is a property: set it after the options exist so a value that is
  // not among them leaves the control blank rather than silently picking one.
  select.value = value;
  return select;
}

export function numberField(id, value, { min, max, step = "0.0001", onInput } = {}) {
  return control(
    "input",
    {
      id,
      name: id,
      type: "number",
      inputmode: "decimal",
      min,
      max,
      step,
      oninput: onInput ? (event) => onInput(event.target.value) : null,
    },
    { value: value === null || value === undefined ? "" : String(value) },
  );
}

export function textField(id, value, { onInput, maxlength, type = "text" } = {}) {
  return control(
    "input",
    {
      id,
      name: id,
      type,
      maxlength,
      spellcheck: "false",
      oninput: onInput ? (event) => onInput(event.target.value) : null,
    },
    { value: value === null || value === undefined ? "" : String(value) },
  );
}

// An error that belongs to one control, not to the page-wide banner which the
// next successful render clears.
export function fieldError(message) {
  return h("p", { class: "field-error", role: "alert", text: message || "" });
}
