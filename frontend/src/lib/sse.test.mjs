// Standalone test of the SSE frame parser — no test framework, just node.
// Run: node src/lib/sse.test.mjs   (exits non-zero on any failure)
//
// These pin the protocol described in the API contract: frames are
// `data: {json}\n\n`, the event `kind` is one of session/text/tool/result/
// error/done, and bytes arrive at arbitrary boundaries via getReader().
import { SSEFrameSplitter, dataFromEventBlock, readEventStream } from './sse.js';

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

// --- dataFromEventBlock ---------------------------------------------------
eq(
  'extracts JSON payload from a data: line',
  dataFromEventBlock('data: {"kind":"text","text":"hi"}'),
  '{"kind":"text","text":"hi"}'
);
eq(
  'strips exactly one space after the colon',
  dataFromEventBlock('data:  leading-space-kept'),
  ' leading-space-kept'
);
eq('ignores comment/heartbeat lines', dataFromEventBlock(': keep-alive'), null);
eq(
  'joins multi-line data fields with newline',
  dataFromEventBlock('data: line1\ndata: line2'),
  'line1\nline2'
);

// --- SSEFrameSplitter: whole frames --------------------------------------
{
  const s = new SSEFrameSplitter();
  const blocks = s.push('data: {"kind":"session","session_id":"abc"}\n\n');
  eq('one complete frame yields one block', blocks, [
    'data: {"kind":"session","session_id":"abc"}',
  ]);
}

// --- SSEFrameSplitter: multiple frames in one chunk ----------------------
{
  const s = new SSEFrameSplitter();
  const blocks = s.push(
    'data: {"kind":"text","text":"a"}\n\ndata: {"kind":"text","text":"b"}\n\n'
  );
  eq('two frames in one chunk yield two blocks', blocks.length, 2);
  eq('first block', dataFromEventBlock(blocks[0]), '{"kind":"text","text":"a"}');
  eq('second block', dataFromEventBlock(blocks[1]), '{"kind":"text","text":"b"}');
}

// --- SSEFrameSplitter: frame split across chunks -------------------------
{
  const s = new SSEFrameSplitter();
  let blocks = s.push('data: {"kind":"te');
  eq('partial frame yields nothing yet', blocks, []);
  blocks = s.push('xt","text":"split"}\n\n');
  eq('completing the frame yields it whole', dataFromEventBlock(blocks[0]), '{"kind":"text","text":"split"}');
}

// --- SSEFrameSplitter: delimiter split across chunks ---------------------
{
  const s = new SSEFrameSplitter();
  let blocks = s.push('data: {"kind":"done"}\n');
  eq('frame held while delimiter incomplete', blocks, []);
  blocks = s.push('\n');
  eq('frame released once blank line completes', dataFromEventBlock(blocks[0]), '{"kind":"done"}');
}

// --- SSEFrameSplitter: CRLF delimiters -----------------------------------
{
  const s = new SSEFrameSplitter();
  const blocks = s.push('data: {"kind":"text","text":"crlf"}\r\n\r\n');
  eq('CRLF-delimited frame parses', dataFromEventBlock(blocks[0]), '{"kind":"text","text":"crlf"}');
}

// --- end-to-end via readEventStream over a mock streaming Response --------
function mockResponse(chunks) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          read() {
            if (i < chunks.length) {
              return Promise.resolve({ value: enc.encode(chunks[i++]), done: false });
            }
            return Promise.resolve({ value: undefined, done: true });
          },
          releaseLock() {},
        };
      },
    },
  };
}

await (async () => {
  // A realistic turn, deliberately chopped at ugly boundaries:
  //  - the session frame split mid-JSON
  //  - two text frames glued together
  //  - a tool frame
  //  - a result frame and the terminal done frame in one chunk
  const chunks = [
    'data: {"kind":"sess',
    'ion","session_id":"S1"}\n\n',
    'data: {"kind":"text","text":"checking "}\n\ndata: {"kind":"text","text":"disk"}\n\n',
    'data: {"kind":"tool","name":"Bash","input":{"command":"df -h"}}\n\n',
    'data: {"kind":"result","is_error":false,"result":"ok","duration_ms":12}\n\ndata: {"kind":"done"}\n\n',
  ];
  const events = [];
  await readEventStream(mockResponse(chunks), (e) => events.push(e));

  eq('event count', events.length, 6);
  eq('1: session id', events[0], { kind: 'session', session_id: 'S1' });
  eq('2: first text', events[1], { kind: 'text', text: 'checking ' });
  eq('3: second text', events[2], { kind: 'text', text: 'disk' });
  eq('4: tool kind+name', { kind: events[3].kind, name: events[3].name }, { kind: 'tool', name: 'Bash' });
  eq('4: tool command', events[3].input.command, 'df -h');
  eq('5: result', events[4], { kind: 'result', is_error: false, result: 'ok', duration_ms: 12 });
  eq('6: done terminal', events[5], { kind: 'done' });
})();

// malformed frame in the middle must be skipped, not abort the stream
await (async () => {
  const chunks = [
    'data: {"kind":"text","text":"before"}\n\n',
    'data: {this is not json}\n\n',
    'data: {"kind":"done"}\n\n',
  ];
  const events = [];
  await readEventStream(mockResponse(chunks), (e) => events.push(e));
  eq('malformed frame skipped, stream continues', events.map((e) => e.kind), ['text', 'done']);
})();

if (failures) {
  console.error(`\n${failures} assertion(s) FAILED`);
  process.exit(1);
}
console.log('\nall SSE parser assertions passed');
