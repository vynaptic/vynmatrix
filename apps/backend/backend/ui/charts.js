// Hand-written SVG charts: a line with crosshair, a sparkline, and diverging bars.
// Marks carry colour; every label and value wears a text token. Each chart has a
// hover and keyboard readout, and callers pair it with a table view.

import { h, svg } from "./format.js";

const tooltip = () => document.getElementById("tooltip");

function showTip(clientX, clientY, value, label) {
  const tip = tooltip();
  if (!tip) return;
  tip.replaceChildren(h("strong", { text: value }), label);
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  const left = Math.min(window.innerWidth - box.width - 8, Math.max(8, clientX + 14));
  const top = clientY - box.height - 14 < 8 ? clientY + 18 : clientY - box.height - 14;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

export function hideTip() {
  const tip = tooltip();
  if (tip) tip.hidden = true;
}

// Draw at the container's real pixel width so text never scales with the panel.
function responsive(draw) {
  const host = h("div", { class: "chart" });
  let drawn = 0;
  const observer = new ResizeObserver(() => {
    if (!host.isConnected) {
      observer.disconnect();
      return;
    }
    const width = Math.max(260, Math.floor(host.clientWidth || 640));
    if (width === drawn) return;
    drawn = width;
    host.replaceChildren(...draw(width));
  });
  observer.observe(host);
  return host;
}

function niceStep(span, count) {
  const raw = span / Math.max(1, count);
  const power = 10 ** Math.floor(Math.log10(raw));
  const unit = raw / power;
  const nice = unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10;
  return nice * power;
}

function ticks(min, max, count = 4) {
  if (min === max) {
    const pad = Math.abs(min) * 0.01 || 1;
    return ticks(min - pad, max + pad, count);
  }
  const step = niceStep(max - min, count);
  const start = Math.floor(min / step) * step;
  const values = [];
  for (let value = start; value <= max + step * 0.5; value += step) values.push(value);
  if (values[values.length - 1] < max) values.push(values[values.length - 1] + step);
  return values;
}

// points: [{ label, short, value }] oldest first. format(value) -> tooltip text,
// tickFormat(value) -> axis text.
export function lineChart(points, options) {
  return responsive((width) => drawLine(points, options, width));
}

function drawLine(points, { format, tickFormat, ariaLabel }, width) {
  const height = 250;
  const margin = { top: 16, right: 22, bottom: 30, left: 64 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;
  const values = points.map((point) => point.value);
  const scaleTicks = ticks(Math.min(...values), Math.max(...values));
  const step = scaleTicks[1] - scaleTicks[0];
  const tickDigits = Math.min(8, Math.max(0, -Math.floor(Math.log10(step))));
  const low = scaleTicks[0];
  const high = scaleTicks[scaleTicks.length - 1];
  const xAt = (index) =>
    margin.left + (points.length === 1 ? innerW / 2 : (index / (points.length - 1)) * innerW);
  const yAt = (value) => margin.top + innerH - ((value - low) / (high - low)) * innerH;

  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${ariaLabel}. Use the left and right arrow keys to read each point.`,
    tabindex: "0",
  });
  for (const tick of scaleTicks) {
    root.append(
      svg("line", {
        class: "chart-grid",
        x1: margin.left,
        x2: width - margin.right,
        y1: yAt(tick),
        y2: yAt(tick),
      }),
      svg("text", {
        class: "chart-tick",
        x: margin.left - 10,
        y: yAt(tick) + 4,
        "text-anchor": "end",
        text: tickFormat ? tickFormat(tick, tickDigits) : format(tick),
      }),
    );
  }
  root.append(
    svg("line", {
      class: "chart-axis",
      x1: margin.left,
      x2: width - margin.right,
      y1: margin.top + innerH,
      y2: margin.top + innerH,
    }),
  );

  const labelAt = new Set([0, Math.floor((points.length - 1) / 2), points.length - 1]);
  for (const index of labelAt) {
    const anchor = index === 0 ? "start" : index === points.length - 1 ? "end" : "middle";
    root.append(
      svg("text", {
        class: "chart-tick",
        x: xAt(index),
        y: height - 8,
        "text-anchor": points.length === 1 ? "middle" : anchor,
        text: points[index].short || points[index].label,
      }),
    );
  }

  if (points.length > 1) {
    const path = points.map((point, index) => `${index ? "L" : "M"}${xAt(index)},${yAt(point.value)}`);
    const floor = margin.top + innerH;
    root.append(
      svg("path", {
        class: "chart-area",
        d: `${path.join(" ")} L${xAt(points.length - 1)},${floor} L${xAt(0)},${floor} Z`,
      }),
      svg("path", { class: "chart-line", d: path.join(" ") }),
    );
  }
  const last = points.length - 1;
  root.append(svg("circle", { class: "chart-dot", cx: xAt(last), cy: yAt(points[last].value), r: 4 }));

  const cross = svg("line", {
    class: "chart-cross",
    y1: margin.top,
    y2: margin.top + innerH,
    visibility: "hidden",
  });
  const marker = svg("circle", { class: "chart-dot", r: 5, visibility: "hidden" });
  const hit = svg("rect", {
    class: "chart-hit",
    x: margin.left,
    y: margin.top,
    width: innerW,
    height: innerH,
  });
  root.append(cross, marker, hit);

  const spoken = h("span", { class: "sr-only", "aria-live": "polite" });
  let active = last;
  const focusPoint = (index, clientX, clientY) => {
    active = Math.min(last, Math.max(0, index));
    const x = xAt(active);
    const y = yAt(points[active].value);
    cross.setAttribute("x1", x);
    cross.setAttribute("x2", x);
    cross.setAttribute("visibility", "visible");
    marker.setAttribute("cx", x);
    marker.setAttribute("cy", y);
    marker.setAttribute("visibility", "visible");
    showTip(clientX, clientY, format(points[active].value), points[active].label);
  };
  const clear = () => {
    cross.setAttribute("visibility", "hidden");
    marker.setAttribute("visibility", "hidden");
    hideTip();
  };
  const screenPoint = (index) => {
    const box = root.getBoundingClientRect();
    return [
      box.left + (xAt(index) / width) * box.width,
      box.top + (yAt(points[index].value) / height) * box.height,
    ];
  };
  hit.addEventListener("pointermove", (event) => {
    const box = root.getBoundingClientRect();
    const x = ((event.clientX - box.left) / box.width) * width;
    const ratio = (x - margin.left) / innerW;
    focusPoint(Math.round(ratio * last), event.clientX, event.clientY);
  });
  hit.addEventListener("pointerleave", clear);
  root.addEventListener("focus", () => focusPoint(active, ...screenPoint(active)));
  root.addEventListener("blur", clear);
  root.addEventListener("keydown", (event) => {
    const move = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!move) return;
    event.preventDefault();
    const next = Math.min(last, Math.max(0, active + move));
    focusPoint(next, ...screenPoint(next));
    spoken.textContent = `${points[next].label}: ${format(points[next].value)}`;
  });
  return [root, spoken];
}

export function sparkline(values, ariaLabel) {
  const width = 160;
  const height = 40;
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;
  const xAt = (index) => 4 + (index / Math.max(1, values.length - 1)) * (width - 8);
  const yAt = (value) => 6 + (1 - (value - low) / span) * (height - 12);
  const path = values.map((value, index) => `${index ? "L" : "M"}${xAt(index)},${yAt(value)}`);
  const last = values.length - 1;
  return svg(
    "svg",
    { class: "tile-spark", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": ariaLabel },
    svg("path", { class: "chart-line", d: path.join(" ") }),
    svg("circle", { class: "chart-dot", cx: xAt(last), cy: yAt(values[last]), r: 4 }),
  );
}

function barPath(x0, x1, y, thickness) {
  // Square at the baseline (x0), 4px rounded at the data end (x1).
  const radius = Math.min(4, Math.abs(x1 - x0), thickness / 2);
  const turn = x1 >= x0 ? radius : -radius;
  return [
    `M${x0},${y}`,
    `L${x1 - turn},${y}`,
    `Q${x1},${y} ${x1},${y + radius}`,
    `L${x1},${y + thickness - radius}`,
    `Q${x1},${y + thickness} ${x1 - turn},${y + thickness}`,
    `L${x0},${y + thickness}`,
    "Z",
  ].join(" ");
}

// rows: [{ label, value }]. Each row is a text line (label left, value right)
// above a bar that diverges from a shared zero baseline. format(value) -> text.
export function divergingBars(rows, options) {
  return responsive((width) => drawBars(rows, options, width));
}

function drawBars(rows, { format, ariaLabel }, width) {
  const pad = 20;
  const rowHeight = 50;
  const thickness = 18;
  const top = 6;
  const height = top + rows.length * rowHeight + 6;
  const values = rows.map((row) => row.value);
  const low = Math.min(0, ...values);
  const high = Math.max(0, ...values);
  const span = high - low || 1;
  const xAt = (value) => pad + ((value - low) / span) * (width - pad * 2);
  const zero = xAt(0);
  const labelRoom = Math.max(8, Math.floor((width - pad * 2 - 110) / 6.4));

  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    height,
    role: "group",
    "aria-label": ariaLabel,
  });
  rows.forEach((row, index) => {
    const y = top + index * rowHeight;
    const barY = y + 24;
    const positive = row.value >= 0;
    const readout = (event) => {
      const box = event.currentTarget.getBoundingClientRect();
      showTip(
        event.clientX ?? box.left + box.width / 2,
        event.clientY ?? box.top,
        format(row.value),
        row.label,
      );
    };
    const hit = svg("rect", {
      class: "chart-hit bar-hit",
      x: 0,
      y,
      width,
      height: rowHeight - 4,
      tabindex: "0",
      role: "img",
      "aria-label": `${row.label}: ${format(row.value)}`,
    });
    hit.addEventListener("pointermove", readout);
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("focus", readout);
    hit.addEventListener("blur", hideTip);
    root.append(
      hit,
      svg("path", {
        class: `bar-mark ${positive ? "bar-gain" : "bar-loss"}`,
        d: barPath(zero, xAt(row.value), barY, thickness),
      }),
      svg("text", { class: "chart-label", x: pad, y: y + 15, text: clip(row.label, labelRoom) }),
      svg("text", {
        class: "chart-value",
        x: width - pad,
        y: y + 15,
        "text-anchor": "end",
        text: format(row.value),
      }),
    );
  });
  root.append(svg("line", { class: "chart-axis", x1: zero, x2: zero, y1: top + 18, y2: height - 4 }));
  return [root];
}

function clip(text, limit) {
  return text.length > limit ? `${text.slice(0, limit - 1)}\u2026` : text;
}
