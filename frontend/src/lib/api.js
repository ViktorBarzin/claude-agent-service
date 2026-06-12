// Same-origin API client. Auth is handled entirely by the edge proxy
// (Authentik / basic-auth / bearer) — this UI never sends or stores a token.
import { readEventStream } from './sse.js';

/** Open a fresh chat session. @returns {Promise<string>} session_id */
export async function openSession() {
  const res = await fetch('/api/session', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
  });
  if (!res.ok) {
    throw new Error(`could not open a session (HTTP ${res.status})`);
  }
  const body = await res.json();
  if (!body || typeof body.session_id !== 'string') {
    throw new Error('session response missing session_id');
  }
  return body.session_id;
}

/**
 * Run one chat turn. Streams events to onEvent until the backend sends
 * {kind:"done"} and the connection closes. Pass an AbortSignal to cancel.
 *
 * @param {{session_id: string, prompt: string, model?: string, signal?: AbortSignal}} opts
 * @param {(event: object) => void} onEvent
 */
export async function streamChat({ session_id, prompt, model, signal }, onEvent) {
  const payload = { session_id, prompt };
  if (model) payload.model = model;

  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      accept: 'text/event-stream',
    },
    body: JSON.stringify(payload),
    signal,
  });
  await readEventStream(res, onEvent);
}

/**
 * List the PVE power verbs and which of them mutate VM state.
 * @returns {Promise<{verbs: string[], mutating: string[]}>}
 */
export async function fetchVerbs() {
  const res = await fetch('/api/pve/verbs');
  if (!res.ok) {
    throw new Error(`could not load VM controls (HTTP ${res.status})`);
  }
  const body = await res.json();
  return {
    verbs: Array.isArray(body.verbs) ? body.verbs : [],
    mutating: Array.isArray(body.mutating) ? body.mutating : [],
  };
}

/**
 * Run a PVE power verb directly (no AI in the path). The backend returns 200
 * on success and 502 when the verb's exit code is non-zero, but the JSON body
 * carries {verb, exit_code, stdout, stderr, rejected} in BOTH cases — so we
 * read the body regardless of HTTP status and let the caller style on
 * exit_code / rejected.
 *
 * @param {string} verb
 * @returns {Promise<{verb: string, exit_code: number|null, stdout: string, stderr: string, rejected: boolean}>}
 */
export async function runVerb(verb) {
  const res = await fetch(`/api/pve/${encodeURIComponent(verb)}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
  });
  // 400 = unknown verb (FastAPI HTTPException) — has {detail}, not the verb shape.
  let body;
  try {
    body = await res.json();
  } catch {
    throw new Error(`VM control '${verb}' failed (HTTP ${res.status}, no body)`);
  }
  if (res.status === 400) {
    throw new Error(body?.detail || `'${verb}' was rejected by the server`);
  }
  return {
    verb: body.verb ?? verb,
    exit_code: body.exit_code ?? null,
    stdout: body.stdout ?? '',
    stderr: body.stderr ?? '',
    rejected: Boolean(body.rejected),
  };
}
