// Talking to the QueryBridge server. Every request carries this browser's session id;
// state-changing requests also carry X-QueryBridge, which the server requires (CSRF protection).

const SESSION_KEY = "qb.session";
let memorySession = null;

function newId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function sessionId() {
  try {
    let id = localStorage.getItem(SESSION_KEY);
    if (!id || !/^[A-Za-z0-9-]{16,64}$/.test(id)) {
      id = newId();
      localStorage.setItem(SESSION_KEY, id);
    }
    return id;
  } catch (e) {
    memorySession = memorySession || newId();
    return memorySession;
  }
}

export class ApiError extends Error {
  constructor(message, code = "error", status = 0) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

const OFFLINE = "QueryBridge is not responding. Check that the app is still running in the terminal window " +
  "where you started it, then try again.";

function headersFor(method, json, accept = "application/json") {
  const headers = { "X-Session-Id": sessionId(), Accept: accept };
  if (method !== "GET") headers["X-QueryBridge"] = "1";
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

async function errorFrom(response) {
  let data = null;
  try { data = await response.json(); } catch (e) { /* not JSON */ }
  const message = data && data.error && data.error.message
    ? data.error.message : `The request failed (status ${response.status}).`;
  return new ApiError(message, data && data.error ? data.error.code : "http", response.status);
}

/** api("/api/x", {method, body, form}) -> parsed JSON (throws ApiError with a friendly message). */
export async function api(path, { method = "GET", body, form, signal } = {}) {
  const json = body !== undefined && !form;
  let response;
  try {
    response = await fetch(path, {
      method, headers: headersFor(method, json), credentials: "same-origin", signal,
      body: form || (json ? JSON.stringify(body) : undefined),
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(OFFLINE, "offline", 0);
  }
  if (!response.ok) throw await errorFrom(response);
  return response.json();
}

/** POST /api/ask and call onEvent for each streamed event; resolves with the final event. */
export async function askStream(payload, onEvent) {
  let response;
  try {
    response = await fetch("/api/ask", {
      method: "POST", headers: headersFor("POST", true, "application/x-ndjson"), credentials: "same-origin",
      body: JSON.stringify(payload),
    });
  } catch (e) {
    throw new ApiError(OFFLINE, "offline", 0);
  }
  if (!response.ok) throw await errorFrom(response);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let last = null;
  const handle = (line) => {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    last = event;
    onEvent(event);
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        handle(buffer.slice(0, index));
        buffer = buffer.slice(index + 1);
      }
    }
    handle(buffer + decoder.decode());
  } catch (e) {
    throw new ApiError("The connection to QueryBridge was interrupted. Please try again.", "stream", 0);
  }
  if (!last || (last.type !== "result" && last.type !== "error")) {
    throw new ApiError("QueryBridge stopped before finishing the answer. Please try again.", "stream", 0);
  }
  return last;
}
