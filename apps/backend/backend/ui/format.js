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

function digitsFor(number, maxDigits) {
  const magnitude = Math.abs(number);
  if (magnitude !== 0 && magnitude < 1) return Math.max(maxDigits, 4);
  return maxDigits;
}

export function fmtNumber(value, { digits = 2, sign = false } = {}) {
  const number = toNumber(value);
  if (number === null) return DASH;
  const places = digitsFor(number, digits);
  const text = new Intl.NumberFormat(undefined, {
    minimumFractionDigits: Math.min(2, places),
    maximumFractionDigits: places,
  }).format(Math.abs(number));
  if (number < 0) return `−${text}`;
  return sign && number > 0 ? `+${text}` : text;
}

// Currency codes here include non-ISO ones (USDC), so the code is a suffix
// rather than an Intl currency style, which rejects them.
export function fmtMoney(value, currency, options = {}) {
  const text = fmtNumber(value, options);
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
  const direction = number > 0 ? "up" : number < 0 ? "down" : "flat";
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
