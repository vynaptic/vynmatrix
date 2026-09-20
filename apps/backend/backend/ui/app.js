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
  {
    id: "settings",
    title: "Settings",
    kicker: "Owner and accounts",
    icon: "M12 15a3 3 0 100-6 3 3 0 000 6zM4 12h2m12 0h2M12 4v2m0 12v2",
    module: "./pages/settings.js",
  },
];

const REFRESH_MS = 30000;
const THEME_KEY = "vynmatrix.theme";

const el = (id) => document.getElementById(id);
// `generation` names the newest request. A response from an older one (the user
// navigated, pressed Lock, or a newer refresh started) is discarded, never drawn.
// `editing` is the shell's only dirty-state concept. While an editor or a
// confirmation is open the timed refresh does not run at all -- focus is not a
// good enough proxy, because a confirmation takes focus out of the form.
const state = {
  page: PAGES[0],
  generation: 0,
  versionShown: false,
  editing: 0,
  resume: null,
};

export function beginEditing() {
  state.editing += 1;
}

export function endEditing() {
  state.editing = Math.max(0, state.editing - 1);
}

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

// Every state-changing action passes through here. It states the current value,
// the new value and the consequence in the owner's language, and nothing is
// committed by a control toggling under the pointer.
//
// `run` is the whole action, captured before any network call, so a 401 mid-way
// loses nothing: the prompt reopens with the same action once the key is back.
export function confirmAction({ title, detail, consequence, confirmLabel, run }) {
  const overlay = el("confirm");
  const error = el("confirm-error");
  const ok = el("confirm-ok");
  const cancel = el("confirm-cancel");
  const opener = document.activeElement;
  beginEditing();

  return new Promise((resolve) => {
    let settled = false;

    function close(result) {
      if (settled) return;
      settled = true;
      overlay.hidden = true;
      el("shell").inert = false;
      endEditing();
      ok.replaceWith(ok.cloneNode(true));
      cancel.replaceWith(cancel.cloneNode(true));
      document.removeEventListener("keydown", onKey);
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
      resolve(result);
    }

    function onKey(event) {
      if (event.key === "Escape") close(false);
    }

    el("confirm-title").textContent = title;
    el("confirm-detail").textContent = detail || "";
    el("confirm-consequence").textContent = consequence || "";
    error.textContent = "";
    error.hidden = true;
    el("confirm-ok").textContent = confirmLabel || "Confirm";
    el("shell").inert = true;
    overlay.hidden = false;

    el("confirm-cancel").addEventListener("click", () => close(false));
    el("confirm-ok").addEventListener("click", async () => {
      const button = el("confirm-ok");
      button.disabled = true;
      try {
        await run();
        close(true);
      } catch (failure) {
        button.disabled = false;
        if (failure instanceof api.ApiError && failure.status === 401) {
          // Hold the action so the owner does not retype anything, then unlock.
          state.resume = { title, detail, consequence, confirmLabel, run };
          close(false);
          showLogin(failure.message);
          return;
        }
        error.textContent = explainWrite(failure);
        error.hidden = false;
      }
    });
    document.addEventListener("keydown", onKey);
    el("confirm-ok").focus();
  });
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

// A write's failure belongs beside the control that caused it, in the owner's
// words. The server returns {"detail": str} only for its own error types, so a
// missing detail must still produce a sentence.
export function explainWrite(error) {
  if (!(error instanceof api.ApiError)) return "Something went wrong while saving.";
  if (error.status === 0) return error.message;
  if (error.status === 409) {
    return error.message || "Someone changed this while you were editing. Reload and try again.";
  }
  if (error.status === 422) return error.message || "That value was not accepted.";
  if (error.status === 404) return error.message || "That is no longer there. Reload the page.";
  if (error.status === 503) return "No deployment owner is set up yet.";
  return error.message || `The change was refused (${error.status}).`;
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
    if (state.resume) {
      const pending = state.resume;
      state.resume = null;
      confirmAction(pending).then((done) => {
        if (done) render();
      });
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
    const idle =
      document.visibilityState === "visible" &&
      el("login").hidden &&
      state.editing === 0 &&
      !readerIsBusy();
    if (idle) render({ quiet: true });
  }, REFRESH_MS);
  route();
  if (state.page.id !== "dashboard") primeSafety();
}

boot();
