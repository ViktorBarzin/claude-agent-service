// transcript.js — the load-bearing core of the breakglass UI.
//
// The attach stream (EventSource) replays the conversation-so-far and then
// tails live. Replayed events are byte-identical to live ones, and on a
// reconnect the server re-replays from Last-Event-ID — so the SAME event id can
// arrive more than once. This module folds a flat, possibly-duplicated event
// sequence into an ordered list of render-ready messages, idempotently.
//
// Contract (every default `message` event's .data is one of these JSON shapes):
//   {kind:"user",      text, id}            → opens a USER bubble
//   {kind:"session",   session_id, id}      → informational (agent's session id)
//   {kind:"text",      text, id}            → assistant prose; concatenated
//   {kind:"tool",      name, input, id}     → inline tool chip (Bash → command)
//   {kind:"result",    is_error, result, duration_ms, id} → closes the bubble
//   {kind:"error",     error, id}           → error note on the bubble
//   {kind:"cancelled", id}                  → muted "stopped" note
//   {kind:"turn_end",  id}                  → the turn finished
//
// Grouping: a `user` event opens a user message; the session/text/tool events
// that follow build ONE assistant message; result/error/cancelled annotate it;
// turn_end ends it. Assistant events with no preceding user (e.g. a session
// banner on a fresh attach) still get an assistant message so nothing is lost.
//
// Idempotency: every event carries a monotonic integer-ish id. We track the
// max id folded so far and DROP any event whose id we've already passed — a
// reconnect replay therefore never double-renders. Ids are compared
// numerically when both parse as numbers, else as strings (defensive).

/** @typedef {{type:'text',text:string}|{type:'tool',name:string,command:string,raw:any}} Part */
/**
 * @typedef {Object} Message
 * @property {'user'|'assistant'} role
 * @property {string} key                 stable key for keyed {#each}
 * @property {string} [text]              user text
 * @property {Part[]} [parts]             assistant parts, in emit order
 * @property {{is_error:boolean,text:string,duration_ms:number|null}} [result]
 * @property {string} [error]
 * @property {boolean} [cancelled]
 * @property {boolean} [ended]            turn_end seen for this message
 */

/** Compare two ids; numeric when both look numeric, else lexicographic. */
export function idGreater(a, b) {
  const na = Number(a);
  const nb = Number(b);
  if (Number.isFinite(na) && Number.isFinite(nb) && `${a}`.trim() !== '' && `${b}`.trim() !== '') {
    return na > nb;
  }
  return String(a) > String(b);
}

/**
 * Create an empty transcript-folding state.
 * @returns {{messages: Message[], maxId: any, sawId: boolean, openAssistant: Message|null, activeUserSeen: boolean}}
 */
export function createTranscript() {
  return {
    messages: [],
    maxId: null,
    sawId: false,
    openAssistant: null,
    // a turn is "active" once a user event (or local prompt) has no following
    // turn_end; the UI reads `active` from reduceEvent's return.
    activeUserSeen: false,
  };
}

function bubbleKey(prefix, id, fallbackIndex) {
  if (id != null && `${id}`.trim() !== '') return `${prefix}:${id}`;
  return `${prefix}:idx:${fallbackIndex}`;
}

/**
 * Should this event be applied, given the max id folded so far? Updates and
 * returns the new max. Events WITHOUT an id are always applied (and don't move
 * the watermark) — the protocol always carries ids, but we never drop data on a
 * malformed frame.
 * @returns {{apply:boolean, maxId:any}}
 */
export function admit(maxId, id) {
  if (id == null || `${id}`.trim() === '') return { apply: true, maxId };
  if (maxId == null) return { apply: true, maxId: id };
  if (idGreater(id, maxId)) return { apply: true, maxId: id };
  return { apply: false, maxId }; // already seen — dedupe
}

/**
 * Fold one event into the transcript state, mutating `state` in place.
 * Returns true if the state changed (so callers can trigger a re-render).
 *
 * @param {ReturnType<typeof createTranscript>} state
 * @param {any} ev parsed event object ({kind, id, ...})
 * @returns {boolean} changed
 */
export function reduceEvent(state, ev) {
  if (!ev || typeof ev !== 'object') return false;
  const { apply, maxId } = admit(state.maxId, ev.id);
  state.maxId = maxId;
  if (!apply) return false;
  if (ev.id != null && `${ev.id}`.trim() !== '') state.sawId = true;

  const ensureAssistant = () => {
    if (!state.openAssistant) {
      const msg = {
        role: 'assistant',
        key: bubbleKey('a', ev.id, state.messages.length),
        parts: [],
        ended: false,
      };
      state.messages.push(msg);
      state.openAssistant = msg;
    }
    return state.openAssistant;
  };

  switch (ev.kind) {
    case 'user': {
      // A new user turn. Close any dangling assistant bubble first.
      state.openAssistant = null;
      state.messages.push({
        role: 'user',
        key: bubbleKey('u', ev.id, state.messages.length),
        text: typeof ev.text === 'string' ? ev.text : '',
      });
      state.activeUserSeen = true;
      return true;
    }
    case 'session': {
      // Informational — does not itself render a part, but it does open the
      // assistant bubble for the turn so subsequent text lands in one place.
      ensureAssistant();
      return true;
    }
    case 'text': {
      if (typeof ev.text !== 'string' || ev.text === '') return false;
      const msg = ensureAssistant();
      const tail = msg.parts[msg.parts.length - 1];
      if (tail && tail.type === 'text') {
        tail.text += ev.text; // concatenate consecutive prose
      } else {
        msg.parts.push({ type: 'text', text: ev.text });
      }
      return true;
    }
    case 'tool': {
      const msg = ensureAssistant();
      const command =
        ev.input && typeof ev.input.command === 'string' ? ev.input.command : '';
      msg.parts.push({
        type: 'tool',
        name: typeof ev.name === 'string' && ev.name ? ev.name : 'tool',
        command,
        raw: ev.input ?? null,
      });
      return true;
    }
    case 'result': {
      const msg = ensureAssistant();
      msg.result = {
        is_error: Boolean(ev.is_error),
        text: typeof ev.result === 'string' ? ev.result : '',
        duration_ms: typeof ev.duration_ms === 'number' ? ev.duration_ms : null,
      };
      return true;
    }
    case 'error': {
      const msg = ensureAssistant();
      msg.error = typeof ev.error === 'string' && ev.error ? ev.error : 'unknown error';
      return true;
    }
    case 'cancelled': {
      const msg = ensureAssistant();
      msg.cancelled = true;
      return true;
    }
    case 'turn_end': {
      if (state.openAssistant) state.openAssistant.ended = true;
      state.openAssistant = null;
      state.activeUserSeen = false;
      return true;
    }
    default:
      return false;
  }
}

/**
 * Convenience: fold an array of events into a fresh transcript (used by tests
 * and by a from-scratch render). Returns the final state.
 * @param {any[]} events
 */
export function foldAll(events) {
  const state = createTranscript();
  for (const ev of events) reduceEvent(state, ev);
  return state;
}
