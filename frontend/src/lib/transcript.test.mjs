// Standalone test of the transcript folder — no test framework, just node.
// Run: node src/lib/transcript.test.mjs   (exits non-zero on any failure)
//
// These pin the attach-model contract: events carry monotonic ids, a reconnect
// re-replays already-seen ids (which MUST be deduped), and events group into
// user/assistant messages with consecutive prose concatenated.
import {
  admit,
  idGreater,
  reduceEvent,
  createTranscript,
  foldAll,
} from './transcript.js';

let failures = 0;
function ok(name, cond) {
  if (cond) {
    console.log(`  ok  ${name}`);
  } else {
    failures++;
    console.error(`FAIL  ${name}`);
  }
}
function eq(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  ok(`${name}  (got ${g})`, g === w);
}

// --- id comparison --------------------------------------------------------
ok('idGreater numeric', idGreater(10, 9) === true);
ok('idGreater numeric not', idGreater(2, 10) === false); // not string "2" > "10"
ok('idGreater string fallback', idGreater('b', 'a') === true);

// --- admit / dedupe watermark --------------------------------------------
{
  let { apply, maxId } = admit(null, 1);
  eq('first id admitted', { apply, maxId }, { apply: true, maxId: 1 });
  ({ apply, maxId } = admit(5, 5));
  ok('equal id rejected (already seen)', apply === false && maxId === 5);
  ({ apply, maxId } = admit(5, 3));
  ok('lower id rejected', apply === false && maxId === 5);
  ({ apply, maxId } = admit(5, 6));
  ok('higher id admitted, watermark moves', apply === true && maxId === 6);
  ({ apply, maxId } = admit(5, undefined));
  ok('id-less event always admitted, watermark held', apply === true && maxId === 5);
}

// --- a full turn groups into user + one assistant bubble ------------------
{
  const events = [
    { kind: 'user', text: 'triage it', id: 1 },
    { kind: 'session', session_id: 'S1', id: 2 },
    { kind: 'text', text: 'Checking ', id: 3 },
    { kind: 'text', text: 'disk usage.', id: 4 },
    { kind: 'tool', name: 'Bash', input: { command: 'df -h' }, id: 5 },
    { kind: 'result', is_error: false, result: 'ok', duration_ms: 1200, id: 6 },
    { kind: 'turn_end', id: 7 },
  ];
  const s = foldAll(events);
  eq('two messages: user + assistant', s.messages.length, 2);
  eq('first is user with text', { r: s.messages[0].role, t: s.messages[0].text }, { r: 'user', t: 'triage it' });
  const a = s.messages[1];
  eq('assistant role', a.role, 'assistant');
  // consecutive text concatenated into ONE part; tool is a separate part
  eq('parts: one concatenated text + one tool', a.parts.map((p) => p.type), ['text', 'tool']);
  eq('prose concatenated in order', a.parts[0].text, 'Checking disk usage.');
  eq('tool command captured', a.parts[1].command, 'df -h');
  eq('result attached', { e: a.result.is_error, ms: a.result.duration_ms }, { e: false, ms: 1200 });
  ok('turn ended', a.ended === true);
  ok('no longer active after turn_end', s.activeUserSeen === false);
}

// --- reconnect replay: re-feeding the SAME events must NOT double-render --
{
  const events = [
    { kind: 'user', text: 'hi', id: 1 },
    { kind: 'text', text: 'hello', id: 2 },
    { kind: 'turn_end', id: 3 },
  ];
  const s = createTranscript();
  for (const e of events) reduceEvent(s, e);
  // simulate an EventSource reconnect that re-replays everything from the top
  for (const e of events) reduceEvent(s, e);
  eq('still exactly two messages after replay', s.messages.length, 2);
  eq('assistant prose not doubled', s.messages[1].parts[0].text, 'hello');
}

// --- a partial replay (Last-Event-ID resume) continues the same bubble ----
{
  const s = createTranscript();
  reduceEvent(s, { kind: 'user', text: 'go', id: 1 });
  reduceEvent(s, { kind: 'text', text: 'part-A ', id: 2 });
  // reconnect: server resumes after id 2; we must drop id<=2 if re-sent and
  // keep appending to the open assistant bubble.
  reduceEvent(s, { kind: 'text', text: 'part-A ', id: 2 }); // dup, dropped
  reduceEvent(s, { kind: 'text', text: 'part-B', id: 3 }); // new, appended
  reduceEvent(s, { kind: 'turn_end', id: 4 });
  eq('resume appended to same bubble', s.messages[1].parts[0].text, 'part-A part-B');
  eq('still two messages', s.messages.length, 2);
}

// --- error / cancelled annotate the open bubble ---------------------------
{
  const s = foldAll([
    { kind: 'user', text: 'x', id: 1 },
    { kind: 'text', text: 'working', id: 2 },
    { kind: 'error', error: 'ssh timeout', id: 3 },
    { kind: 'turn_end', id: 4 },
  ]);
  eq('error note on assistant bubble', s.messages[1].error, 'ssh timeout');
}
{
  const s = foldAll([
    { kind: 'user', text: 'x', id: 1 },
    { kind: 'cancelled', id: 2 },
    { kind: 'turn_end', id: 3 },
  ]);
  ok('cancelled flag on assistant bubble', s.messages[1].cancelled === true);
}

// --- active state: a user event with no turn_end means a turn is running ---
{
  const s = createTranscript();
  reduceEvent(s, { kind: 'user', text: 'go', id: 1 });
  reduceEvent(s, { kind: 'text', text: '...', id: 2 });
  ok('active while no turn_end', s.activeUserSeen === true);
  reduceEvent(s, { kind: 'turn_end', id: 3 });
  ok('inactive after turn_end', s.activeUserSeen === false);
}

// --- assistant-only stream (session banner on a fresh attach) still renders -
{
  const s = foldAll([
    { kind: 'session', session_id: 'S1', id: 1 },
    { kind: 'text', text: 'standing by', id: 2 },
    { kind: 'turn_end', id: 3 },
  ]);
  eq('lone assistant message created', s.messages.length, 1);
  eq('assistant prose present', s.messages[0].parts[0].text, 'standing by');
}

// --- two sequential turns produce two assistant bubbles -------------------
{
  const s = foldAll([
    { kind: 'user', text: 'q1', id: 1 },
    { kind: 'text', text: 'a1', id: 2 },
    { kind: 'turn_end', id: 3 },
    { kind: 'user', text: 'q2', id: 4 },
    { kind: 'text', text: 'a2', id: 5 },
    { kind: 'turn_end', id: 6 },
  ]);
  eq('four messages (u,a,u,a)', s.messages.map((m) => m.role), ['user', 'assistant', 'user', 'assistant']);
  eq('second answer in its own bubble', s.messages[3].parts[0].text, 'a2');
  ok('message keys are unique', new Set(s.messages.map((m) => m.key)).size === 4);
}

if (failures) {
  console.error(`\n${failures} assertion(s) FAILED`);
  process.exit(1);
}
console.log('\nall transcript assertions passed');
