// One place that talks to the API. Every failure surfaces the server's own message, because the
// API already names a cause and a fix and re-writing that in the client would lose both.

const BASE = "/api";

function token() {
  return import.meta.env.VITE_LEDGERLINE_TOKEN ?? "dev-token";
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(BASE + path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token()}`,
        ...(options.headers ?? {}),
      },
    });
  } catch (cause) {
    throw new ApiError(
      "Could not reach the API. Check that it is running on port 8000.",
      0,
      cause,
    );
  }

  const body = await response.text();
  let parsed = null;
  if (body) {
    try {
      parsed = JSON.parse(body);
    } catch {
      throw new ApiError(`The API returned something that is not JSON: ${body.slice(0, 200)}`, response.status);
    }
  }

  if (!response.ok) {
    throw new ApiError(detailOf(parsed) ?? `Request failed with ${response.status}.`, response.status, parsed);
  }
  return parsed;
}

function detailOf(payload) {
  if (!payload) return null;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail.map((d) => d.msg ?? JSON.stringify(d)).join("; ");
  }
  return null;
}

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
  get isVersionConflict() {
    return this.status === 409;
  }
}

async function upload(path, file) {
  const form = new FormData();
  form.append("file", file);
  let response;
  try {
    response = await fetch(BASE + path, {
      method: "POST",
      headers: { Authorization: `Bearer ${token()}` },
      body: form,
    });
  } catch (cause) {
    throw new ApiError("Could not reach the API. Check that it is running on port 8000.", 0, cause);
  }
  const parsed = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(detailOf(parsed) ?? `Upload failed with ${response.status}.`, response.status, parsed);
  }
  return parsed;
}

export const api = {
  piles: () => request("/piles"),
  createPile: (name) =>
    request("/piles", { method: "POST", body: JSON.stringify({ name }) }),
  uploadDocument: (pileId, file) => upload(`/piles/${pileId}/documents`, file),
  startRun: (pileId, documentIds) =>
    request(`/piles/${pileId}/runs`, {
      method: "POST",
      body: JSON.stringify(documentIds ?? []),
    }),
  documents: (pileId) => request(`/piles/${pileId}/documents`),
  document: (documentId) => request(`/documents/${documentId}`),
  latestRun: (pileId) => request(`/piles/${pileId}/latest-run`),
  pending: (pileId) => request(`/piles/${pileId}/pending`),
  register: (pileId) => request(`/piles/${pileId}/register`),
  ledger: (pileId) => request(`/piles/${pileId}/ledger`),
  span: (documentId, start, end) =>
    request(`/documents/${documentId}/span?start=${start}&end=${end}`),

  approve: (pileId, itemIds, decidedBy) =>
    request(`/piles/${pileId}/approve`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds, decided_by: decidedBy }),
    }),
  reject: (pileId, itemIds, decidedBy) =>
    request(`/piles/${pileId}/reject`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds, decided_by: decidedBy }),
    }),
  commit: (pileId, runId, expectedVersion, committedBy) =>
    request(`/piles/${pileId}/commit`, {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        expected_version: expectedVersion,
        committed_by: committedBy,
      }),
    }),
};

// Proposed-change payloads arrive as JSON-encoded strings on some surfaces and as objects on
// others. Parsing defensively in one place is what stops every field reading as undefined further
// down; doing it at each use site is how that bug spreads.
export function asObject(value) {
  if (value == null) return null;
  if (typeof value === "object") return value;
  if (typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  }
  return null;
}

export function formatValue(value) {
  if (value === null || value === undefined) return "not stated";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
