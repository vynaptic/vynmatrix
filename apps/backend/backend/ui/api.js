// Read-only client for /api/ui. The admin key lives in sessionStorage for this
// tab only, travels solely in the X-Admin-Key header, and is dropped on any 401.

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
  const headers = { Accept: "application/json" };
  const key = read() || volatileKey;
  if (key) headers["X-Admin-Key"] = key;

  let response;
  try {
    response = await fetch(url, { headers, cache: "no-store", credentials: "omit" });
  } catch {
    throw new ApiError(0, "The platform is not answering. Check that it is running.");
  }
  if (response.status === 401) {
    clearKey();
    throw new ApiError(401, "That admin key was not accepted.");
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : null;
    throw new ApiError(response.status, detail);
  }
  return body;
}
