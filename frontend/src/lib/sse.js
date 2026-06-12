// SSE frame parsing — the load-bearing core of the breakglass UI.
//
// The /api/chat endpoint returns a text/event-stream that we read with
// fetch() + response.body.getReader() (NOT EventSource, which cannot POST).
// The backend emits one frame per event as:
//
//     data: {json}\n\n
//
// getReader() hands us bytes at arbitrary boundaries: a single frame can be
// split across reads, and one read can contain several frames. So we keep a
// rolling text buffer, split it on the blank-line frame delimiter, and only
// hand back the JSON payload of *complete* frames. Per the SSE spec a frame may
// carry multiple `data:` lines (joined with "\n"); the backend emits single
// line JSON today, but we handle the general case so a future multi-line
// payload can't silently corrupt the stream.

/**
 * Parse a single SSE event block (the text between blank lines) into its data
 * payload string, or null if the block carries no `data:` field (e.g. a bare
 * comment or a `:` heartbeat).
 * @param {string} block
 * @returns {string|null}
 */
export function dataFromEventBlock(block) {
  const dataLines = [];
  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (line.startsWith(':')) continue; // SSE comment / heartbeat
    if (line === 'data:' || line === 'data') {
      dataLines.push('');
    } else if (line.startsWith('data:')) {
      // Spec: a single leading space after the colon is stripped.
      let v = line.slice('data:'.length);
      if (v.startsWith(' ')) v = v.slice(1);
      dataLines.push(v);
    }
    // field lines we don't care about (event:, id:, retry:) are ignored
  }
  if (dataLines.length === 0) return null;
  return dataLines.join('\n');
}

/**
 * A stateful splitter that turns an arbitrary sequence of decoded text chunks
 * into a sequence of complete SSE event-block strings. Frames are delimited by
 * a blank line; we tolerate both "\n\n" and "\r\n\r\n".
 */
export class SSEFrameSplitter {
  constructor() {
    this.buffer = '';
  }

  /**
   * Feed a decoded text chunk; returns the event blocks that are now complete.
   * Any trailing partial frame stays buffered for the next chunk.
   * @param {string} chunk
   * @returns {string[]} complete event blocks (text between delimiters)
   */
  push(chunk) {
    this.buffer += chunk;
    const blocks = [];
    // Normalise CRLF delimiters to LF so a single split rule covers both.
    let idx;
    // Process every complete frame currently in the buffer.
    while ((idx = this._nextDelimiter()) !== -1) {
      const block = this.buffer.slice(0, idx.start);
      this.buffer = this.buffer.slice(idx.end);
      if (block.length > 0) blocks.push(block);
    }
    return blocks;
  }

  /**
   * On stream end, return whatever complete-looking content remains. A
   * well-behaved backend always terminates the last frame with a blank line,
   * so this is usually empty — but if the connection closed mid-trailing-frame
   * with a parseable block, surface it rather than dropping data.
   * @returns {string[]}
   */
  flush() {
    const rest = this.buffer.trim();
    this.buffer = '';
    return rest ? [rest] : [];
  }

  _nextDelimiter() {
    // Find the earliest of "\n\n", "\r\n\r\n", "\r\r".
    const candidates = [
      { token: '\r\n\r\n', i: this.buffer.indexOf('\r\n\r\n') },
      { token: '\n\n', i: this.buffer.indexOf('\n\n') },
      { token: '\r\r', i: this.buffer.indexOf('\r\r') },
    ].filter((c) => c.i !== -1);
    if (candidates.length === 0) return -1;
    candidates.sort((a, b) => a.i - b.i);
    const { token, i } = candidates[0];
    return { start: i, end: i + token.length };
  }
}

/**
 * Read an SSE Response body to completion, invoking onEvent for every parsed
 * JSON event object. Resolves when the stream ends. Throws if the response is
 * not ok or has no readable body (caller shows the error inline).
 *
 * @param {Response} response  a fetch() Response with a streaming body
 * @param {(event: object) => void} onEvent  called per parsed JSON event
 */
export async function readEventStream(response, onEvent) {
  if (!response.ok) {
    throw new Error(`server returned ${response.status} ${response.statusText}`);
  }
  if (!response.body) {
    throw new Error('response has no readable body (streaming unsupported)');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const splitter = new SSEFrameSplitter();

  const handleBlock = (block) => {
    const payload = dataFromEventBlock(block);
    if (payload == null || payload.trim() === '') return;
    let obj;
    try {
      obj = JSON.parse(payload);
    } catch {
      // A malformed frame must not abort an in-progress recovery stream;
      // skip it and keep reading.
      return;
    }
    onEvent(obj);
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const text = decoder.decode(value, { stream: true });
      for (const block of splitter.push(text)) handleBlock(block);
    }
  } finally {
    reader.releaseLock?.();
  }
  // Drain any trailing bytes the decoder held, then any final frame.
  const tail = decoder.decode();
  if (tail) {
    for (const block of splitter.push(tail)) handleBlock(block);
  }
  for (const block of splitter.flush()) handleBlock(block);
}
