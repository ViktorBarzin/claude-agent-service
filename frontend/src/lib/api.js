// Same-origin API client for the breakglass UI.
//
// Auth is handled entirely by the edge proxy (Authentik / basic-auth / bearer):
// this UI never sends or stores a token, and builds no login screen.
//
// The chat uses the tmux/attach model. The conversation lives SERVER-SIDE; we
// only persist the session_id locally and ATTACH to it over an EventSource. The
// browser's native EventSource auto-reconnects and sends Last-Event-ID, and the
// server resumes from there — so there is ZERO reconnect logic here. We just
// render events idempotently by id (see transcript.js).

const SESSION_KEY = 'breakglass.session_id';

/** Read the persisted session id, or '' if none. */
export function loadSessionId() {
  try {
    return localStorage.getItem(SESSION_KEY) || '';
  } catch {
    return '';
  }
}

/** Persist the session id (best-effort; private-mode storage may throw). */
export function saveSessionId(id) {
  try {
    if (id) localStorage.setItem(SESSION_KEY, id);
    else localStorage.removeItem(SESSION_KEY);
  } catch {
    /* ignore — storage is a convenience, not a requirement */
  }
}

/** Forget the persisted session id (the "New session" archive step). */
export function clearSessionId() {
  saveSessionId('');
}

/** Open a fresh server-side session. @returns {Promise<string>} session_id */
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
 * Attach to a session's event stream. Returns the live EventSource so the
 * caller can close() it. Events arrive as:
 *   - default `message` events: .data is JSON {kind, id, ...}
 *   - a named `caught-up` event once the replay is drained (.data is {})
 *   - native `error` events while reconnecting (EventSource retries itself)
 *
 * @param {string} sessionId
 * @param {{
 *   onEvent: (e: object) => void,
 *   onCaughtUp?: () => void,
 *   onOpen?: () => void,
 *   onError?: (e: Event) => void,
 * }} handlers
 * @returns {EventSource}
 */
export function attachStream(sessionId, { onEvent, onCaughtUp, onOpen, onError }) {
  const es = new EventSource(`/api/session/${encodeURIComponent(sessionId)}/stream`);

  es.onopen = () => onOpen?.();

  es.onmessage = (e) => {
    if (!e || typeof e.data !== 'string' || e.data === '') return;
    let obj;
    try {
      obj = JSON.parse(e.data);
    } catch {
      // A malformed frame must not abort an in-progress recovery stream.
      return;
    }
    // EventSource exposes the SSE `id:` line as e.lastEventId. The server also
    // embeds id in the JSON; prefer the JSON id, fall back to lastEventId.
    if ((obj.id == null || obj.id === '') && e.lastEventId) obj.id = e.lastEventId;
    onEvent(obj);
  };

  es.addEventListener('caught-up', () => onCaughtUp?.());

  es.onerror = (e) => {
    // EventSource auto-reconnects on a transient drop (readyState CONNECTING);
    // we only surface a hard, terminal failure (readyState CLOSED).
    onError?.(e);
  };

  return es;
}

/**
 * Start a turn. Output arrives via the attach stream, NOT this response.
 * @param {{session_id: string, prompt: string, model?: string}} opts
 * @returns {Promise<{status:'started'|'busy'|'gone'}>}
 *   started — accepted; busy — 409 (a turn already runs); gone — 404 (re-create).
 */
export async function sendPrompt({ session_id, prompt, model }) {
  const payload = { prompt };
  if (model) payload.model = model;
  const res = await fetch(`/api/session/${encodeURIComponent(session_id)}/prompt`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (res.status === 409) return { status: 'busy' };
  if (res.status === 404) return { status: 'gone' };
  if (!res.ok) throw new Error(`could not start the turn (HTTP ${res.status})`);
  return { status: 'started' };
}

/**
 * Cancel the in-flight turn (the Stop button).
 * @param {string} sessionId
 * @returns {Promise<boolean>} whether a turn was cancelled
 */
export async function cancelTurn(sessionId) {
  const res = await fetch(`/api/session/${encodeURIComponent(sessionId)}/cancel`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
  });
  if (!res.ok) throw new Error(`could not stop the turn (HTTP ${res.status})`);
  const body = await res.json().catch(() => ({}));
  return Boolean(body.cancelled);
}

/**
 * List the PVE power verbs and which mutate VM state.
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
 * Run a PVE power verb directly (no AI in the path). The backend returns 200 on
 * success and 502 when the verb's exit code is non-zero, but the JSON body
 * carries {verb, exit_code, stdout, stderr, rejected} in BOTH cases — so we read
 * the body regardless of HTTP status and let the caller style on exit_code.
 *
 * @param {string} verb
 * @returns {Promise<{verb:string, exit_code:number|null, stdout:string, stderr:string, rejected:boolean}>}
 */
export async function runVerb(verb) {
  const res = await fetch(`/api/pve/${encodeURIComponent(verb)}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
  });
  let body;
  try {
    body = await res.json();
  } catch {
    throw new Error(`VM control '${verb}' failed (HTTP ${res.status}, no body)`);
  }
  // 400 = unknown verb (FastAPI HTTPException) — has {detail}, not the verb shape.
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
