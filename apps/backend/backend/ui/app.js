// Shell: page registry, hash router, unlock flow, refresh loop and theme.
// To add a page: drop a module in ./pages exporting load(api) and view(data, ctx),
// add one entry to PAGES, and add its route in backend/ui_api.py.

import * as api from "./api.js";
import { hideTip } from "./charts.js";
import { fmtClock, fmtDateTime, h, svg } from "./format.js";

const PAGES = [
  {
    id: "dashboard",
    title: "Dashboard",
    kicker: "Overview",
    icon: "M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-3H4zM14 7h6V4h-6z",
    module: "./pages/dashboard.js",
  },
  {
    id: "strategies",
    title: "Strategies",
    kicker: "Catalogue and bindings",
    icon: "M4 18l5-6 4 3 7-9M15 6h5v5",
    module: "./pages/strategies.js",
  },
  {
    id: "pnl",
    title: "Profit and loss",
    kicker: "Account performance",
    icon: "M4 20V4M4 20h16M8 16v-4M12 16V8M16 16v-6",
    module: "./pages/pnl.js",
  },
];

const REFRESH_MS = 30000;
const THEME_KEY = "vynmatrix.theme";

const el = (id) => document.getElementById(id);
// `generation` names the newest request. A response from an older one (the user
// navigated, pressed Lock, or a newer refresh started) is discarded, never drawn.
const state = { page: PAGES[0], generation: 0, versionShown: false };

function currentPage() {
  const id = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  return PAGES.find((page) => page.id === id) || PAGES[0];
}

function buildNav() {
  el("nav").replaceChildren(
    ...PAGES.map((page) =>
      h(
        "a",
        { href: `#/${page.id}`, "data-page": page.id },
        svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d: page.icon })),
        page.title,
      ),
    ),
  );
}

function markNav() {
  for (const link of el("nav").querySelectorAll("a")) {
    if (link.dataset.page === state.page.id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function showBanner(message) {
  const banner = el("banner");
  banner.textContent = message || "";
  banner.hidden = !message;
}

function showSafety(safety) {
  const badge = el("mode-badge");
  if (!safety) return;
  const paperOnly = safety.execution_mode === "paper" && safety.allow_live === false;
  badge.className = paperOnly ? "badge badge-paper" : "badge badge-danger";
  badge.textContent = paperOnly ? "Paper · live orders disabled" : "Live orders possible";
  badge.hidden = false;
}

// The unlock prompt is modal: what is behind it is emptied, inert and unread.
function showLogin(message) {
  state.generation += 1;
  state.versionShown = false;
  hideTip();
  el("version").hidden = true;
  el("main").replaceChildren();
  el("main").classList.remove("is-loading");
  el("shell").inert = true;
  const error = el("login-error");
  error.textContent = message || "";
  error.hidden = !message;
  el("login").hidden = false;
  el("lock").hidden = true;
  el("login-key").value = "";
  el("login-key").focus();
}

function hideLogin() {
  const wasShown = !el("login").hidden;
  el("login").hidden = true;
  el("shell").inert = false;
  return wasShown;
}

function explain(error) {
  if (error.status === 503) {
    return "No deployment owner is set up yet. Finish the first-run setup, then reload this page.";
  }
  if (error.status === 0) return error.message;
  return `Could not refresh (${error.message}). Showing what was loaded last.`;
}

// quiet: a timed refresh. It neither dims the page nor replays the entry animation.
async function render({ quiet = false } = {}) {
  state.generation += 1;
  const generation = state.generation;
  const page = state.page;
  const hadKey = api.hasKey();
  const main = el("main");
  if (!quiet) main.classList.add("is-loading");
  try {
    const module = await import(page.module);
    const data = await module.load(api);
    if (generation !== state.generation) return;
    hideTip();
    main.classList.toggle("is-fresh", !quiet);
    main.replaceChildren(...module.view(data, { api, refresh: render }));
    if (data && data.safety) showSafety(data.safety);
    el("updated").textContent = `Updated ${fmtClock(new Date())}`;
    el("lock").hidden = !api.hasKey();
    if (hideLogin()) main.focus();
    if (!state.versionShown) {
      state.versionShown = true;
      showVersion();
    }
    showBanner(null);
  } catch (error) {
    if (generation !== state.generation) return;
    if (error instanceof api.ApiError && error.status === 401) {
      // Only a key that was actually presented can have been refused.
      showLogin(hadKey ? error.message : null);
    } else if (error instanceof api.ApiError) {
      showBanner(explain(error));
    } else {
      showBanner("Something went wrong while drawing this page.");
      throw error;
    }
  } finally {
    if (generation === state.generation) main.classList.remove("is-loading");
  }
}

function route() {
  state.page = currentPage();
  document.title = `${state.page.title} · vynmatrix`;
  el("page-title").textContent = state.page.title;
  el("page-kicker").textContent = state.page.kicker;
  markNav();
  render();
}

// Which build is running. Read once per unlock: it only changes on a deploy.
async function showVersion() {
  const node = el("version");
  let data;
  try {
    data = await api.get("version");
  } catch {
    node.hidden = true;
    return;
  }
  const parts = [];
  const image = data.image;
  const deployment = data.deployment;
  if (image && image.source_commit) {
    parts.push(`Build ${image.source_commit.slice(0, 12)}${image.source_dirty ? " (uncommitted changes)" : ""}`);
  }
  if (deployment) {
    parts.push(`schema ${deployment.alembic_head}`);
    parts.push(`installed ${fmtDateTime(deployment.started_at)}`);
    if (deployment.outcome !== "succeeded") parts.push(`last deployment ${deployment.outcome.replace("_", " ")}`);
  } else {
    parts.push("no deployment recorded");
  }
  if (data.drift === true) {
    parts.push("the running image is not the one that was deployed");
  }
  node.dataset.drift = String(data.drift === true);
  node.textContent = parts.join(" · ");
  node.hidden = parts.length === 0;
}

async function primeSafety() {
  try {
    showSafety((await api.get("overview")).safety);
  } catch {
    // The page render reports the problem; the badge simply stays hidden.
  }
}

// A timed refresh must not pull the page out from under someone using it.
function readerIsBusy() {
  const main = el("main");
  return main.contains(document.activeElement) || main.querySelector("details[open]") !== null;
}

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function initTheme() {
  let stored = null;
  try {
    stored = window.localStorage.getItem(THEME_KEY);
  } catch {
    stored = null;
  }
  applyTheme(stored);
  el("theme").addEventListener("click", () => {
    const dark =
      document.documentElement.dataset.theme === "dark" ||
      (!document.documentElement.dataset.theme &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark";
    applyTheme(next);
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch {
      // The choice then lasts for this visit only.
    }
  });
}

function boot() {
  buildNav();
  initTheme();
  el("refresh").addEventListener("click", () => render());
  el("lock").addEventListener("click", () => {
    api.clearKey();
    showLogin(null);
  });
  el("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = el("login-key").value.trim();
    if (!value) return;
    api.setKey(value);
    await render();
    if (el("login").hidden && state.page.id !== "dashboard") primeSafety();
  });
  window.addEventListener("hashchange", route);
  window.setInterval(() => {
    const idle = document.visibilityState === "visible" && el("login").hidden && !readerIsBusy();
    if (idle) render({ quiet: true });
  }, REFRESH_MS);
  route();
  if (state.page.id !== "dashboard") primeSafety();
}

boot();
