// Client for /api/ui and the owner control routes. The admin key lives in
// sessionStorage for this tab only, travels solely in the X-Admin-Key header,
// and is dropped on any 401. Writes are fetch-based because the page's CSP sets
// form-action 'none' -- a native form submission cannot leave this document.

const STORAGE_KEY = "vynmatrix.adminKey";

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Request failed (${status})`);
    this.status = status;
  }
}

// Fallback for browsers that refuse site storage: the key then lives in memory.
let volatileKey = null;

function read() {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function hasKey() {
  return Boolean(read() || volatileKey);
}

export function setKey(value) {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, value);
  } catch {
    // Storage can be unavailable; the in-memory copy below still unlocks this tab.
  }
  volatileKey = value;
}

export function clearKey() {
  volatileKey = null;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing stored, nothing to clear.
  }
}

export async function get(path, params = {}) {
  const url = new URL(`/api/ui/${path}`, window.location.origin);
  for (const [name, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) url.searchParams.set(name, String(value));
  }
  return request(url, { method: "GET" });
}

// A state-changing call. `path` is absolute so the two routes that already own
// the owner profile and broker accounts (/owner, /broker-accounts/:id) can be
// reused rather than mirrored under /api/ui.
export async function send(method, path, body) {
  const url = new URL(path, window.location.origin);
  return request(url, {
    method,
    body: JSON.stringify(body),
    extraHeaders: { "Content-Type": "application/json" },
  });
}

async function request(url, { method, body, extraHeaders = {} }) {
  const headers = { Accept: "application/json", ...extraHeaders };
  const key = read() || volatileKey;
  if (key) headers["X-Admin-Key"] = key;

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body,
      cache: "no-store",
      credentials: "omit",
    });
  } catch {
    throw new ApiError(0, "The platform is not answering. Check that it is running.");
  }
  if (response.status === 401) {
    clearKey();
    throw new ApiError(401, "That admin key was not accepted.");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && typeof payload.detail === "string" ? payload.detail : null;
    throw new ApiError(response.status, detail);
  }
  return payload;
}
