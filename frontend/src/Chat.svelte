<script>
  import { tick } from 'svelte';
  import { streamChat } from './lib/api.js';
  import ToolChip from './ToolChip.svelte';

  let {
    sessionId = '',
    sessionReady = false,
    onLiveSession = (/** @type {string} */ _id) => {},
    onStreamingChange = (/** @type {boolean} */ _v) => {},
  } = $props();

  /**
   * Message model. A user message is plain text. An assistant message is an
   * ordered list of parts so streamed prose and tool chips interleave in the
   * exact order the agent emitted them:
   *   { role:'assistant', parts:[{type:'text',text}|{type:'tool',name,command}],
   *     result?: {is_error, text, duration_ms}, error?: string }
   * @type {Array<any>}
   */
  let messages = $state([]);
  let draft = $state('');
  let streaming = $state(false);
  let scroller; // the scroll viewport
  let inputEl;
  let pinnedToBottom = true; // auto-scroll only while the user is at the bottom

  const canSend = $derived(sessionReady && !streaming && draft.trim().length > 0);

  // ── scrolling ─────────────────────────────────────────────────────────────
  function onScroll() {
    if (!scroller) return;
    const gap = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    pinnedToBottom = gap < 60;
  }
  async function scrollToBottom(force = false) {
    if (!force && !pinnedToBottom) return;
    await tick();
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  // ── streaming a turn ────────────────────────────────────────────────────────
  function lastAssistant() {
    return messages[messages.length - 1];
  }

  function appendText(text) {
    const msg = lastAssistant();
    const parts = msg.parts;
    const tail = parts[parts.length - 1];
    if (tail && tail.type === 'text') {
      tail.text += text;
    } else {
      parts.push({ type: 'text', text });
    }
    messages = messages; // notify Svelte of the in-place mutation
  }

  function handleEvent(ev) {
    switch (ev?.kind) {
      case 'session':
        onLiveSession(ev.session_id);
        break;
      case 'text':
        if (ev.text) appendText(ev.text);
        break;
      case 'tool': {
        // Bash carries a `command`; other tools just show their name.
        const command =
          ev.input && typeof ev.input.command === 'string' ? ev.input.command : '';
        lastAssistant().parts.push({ type: 'tool', name: ev.name || 'tool', command });
        messages = messages;
        break;
      }
      case 'result':
        lastAssistant().result = {
          is_error: Boolean(ev.is_error),
          text: typeof ev.result === 'string' ? ev.result : '',
          duration_ms: typeof ev.duration_ms === 'number' ? ev.duration_ms : null,
        };
        messages = messages;
        break;
      case 'error':
        lastAssistant().error = ev.error || 'unknown error';
        messages = messages;
        break;
      case 'done':
        // handled by the stream completing; nothing to render
        break;
      default:
        break;
    }
    scrollToBottom();
  }

  async function send() {
    const prompt = draft.trim();
    if (!prompt || streaming || !sessionReady) return;

    messages.push({ role: 'user', text: prompt });
    messages.push({ role: 'assistant', parts: [] });
    messages = messages;
    draft = '';
    streaming = true;
    onStreamingChange(true);
    pinnedToBottom = true;
    await scrollToBottom(true);

    try {
      await streamChat({ session_id: sessionId, prompt }, handleEvent);
    } catch (err) {
      // Network/transport failure (backend down, connection dropped mid-stream).
      const msg = lastAssistant();
      if (msg && msg.role === 'assistant' && !msg.error) {
        msg.error =
          (err instanceof Error ? err.message : String(err)) +
          ' — the connection to the agent failed.';
        messages = messages;
      }
    } finally {
      streaming = false;
      onStreamingChange(false);
      await scrollToBottom();
      inputEl?.focus();
    }
  }

  function onKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
    // Shift+Enter falls through to insert a newline.
  }

  function fmtDuration(ms) {
    if (ms == null) return '';
    if (ms < 1000) return `${ms} ms`;
    return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
  }

  const isEmpty = $derived(messages.length === 0);
</script>

<div class="chat">
  <div class="chat-head">
    <span class="chat-head-label">Recovery agent</span>
    <span class="chat-head-hint">SSHes into the devvm to diagnose &amp; repair</span>
  </div>

  <div class="stream" bind:this={scroller} onscroll={onScroll}>
    {#if isEmpty}
      <div class="empty">
        <div class="empty-mark">⌁</div>
        <p class="empty-title">The agent is standing by.</p>
        <p class="empty-sub">
          Describe the symptom — "devvm is unreachable", "disk full", "ssh hangs"
          — and it will connect over SSH, investigate, and stream its work here.
          For a hard power action when the agent can't help, use
          <strong>Direct VM control</strong>.
        </p>
      </div>
    {/if}

    {#each messages as msg, i (i)}
      {#if msg.role === 'user'}
        <div class="row row--user">
          <div class="bubble bubble--user">{msg.text}</div>
        </div>
      {:else}
        <div class="row row--assistant">
          <div class="bubble bubble--assistant">
            {#if msg.parts.length === 0 && !msg.result && !msg.error}
              <span class="thinking" aria-label="working">
                <span></span><span></span><span></span>
              </span>
            {/if}
            {#each msg.parts as part, j (j)}
              {#if part.type === 'text'}
                <span class="prose">{part.text}</span>
              {:else}
                <ToolChip name={part.name} command={part.command} />
              {/if}
            {/each}

            {#if msg.error}
              <div class="turn-note turn-note--error">⚠ {msg.error}</div>
            {:else if msg.result}
              <div class="turn-note {msg.result.is_error ? 'turn-note--error' : 'turn-note--ok'}">
                <span class="turn-note-tag">{msg.result.is_error ? 'failed' : 'done'}</span>
                {#if msg.result.text}<span class="turn-note-body">{msg.result.text}</span>{/if}
                {#if msg.result.duration_ms != null}
                  <span class="turn-note-time">{fmtDuration(msg.result.duration_ms)}</span>
                {/if}
              </div>
            {/if}
          </div>
        </div>
      {/if}
    {/each}
  </div>

  <form
    class="composer"
    onsubmit={(e) => {
      e.preventDefault();
      send();
    }}
  >
    {#if streaming}
      <div class="working-bar" aria-live="polite">
        <span class="working-dots"><span></span><span></span><span></span></span>
        agent working — streaming live
      </div>
    {/if}
    <div class="composer-row">
      <textarea
        bind:this={inputEl}
        bind:value={draft}
        onkeydown={onKeydown}
        placeholder={sessionReady
          ? 'Describe the problem…  (Enter to send · Shift+Enter for a new line)'
          : 'Waiting for a session…'}
        rows="1"
        disabled={!sessionReady || streaming}
        spellcheck="false"
      ></textarea>
      <button type="submit" class="send" disabled={!canSend}>
        {streaming ? '…' : 'Send'}
      </button>
    </div>
  </form>
</div>

<style>
  .chat {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: var(--shadow-panel);
    overflow: hidden;
  }

  .chat-head {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 13px 18px;
    border-bottom: 1px solid var(--line);
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.015), transparent);
  }
  .chat-head-label {
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: var(--cyan);
  }
  .chat-head-hint {
    font-size: 12px;
    color: var(--ink-faint);
  }

  .stream {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 20px 18px 8px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    scroll-behavior: smooth;
  }

  /* empty state */
  .empty {
    margin: auto;
    max-width: 460px;
    text-align: center;
    padding: 28px 12px;
    color: var(--ink-dim);
  }
  .empty-mark {
    font-size: 40px;
    color: var(--cyan-dim);
    line-height: 1;
    margin-bottom: 14px;
    text-shadow: 0 0 24px rgba(61, 209, 214, 0.25);
  }
  .empty-title {
    font-family: var(--mono);
    color: var(--ink);
    font-size: 15px;
    margin: 0 0 8px;
  }
  .empty-sub {
    font-size: 13px;
    line-height: 1.6;
    color: var(--ink-faint);
    margin: 0;
  }
  .empty-sub strong {
    color: var(--ink-dim);
    font-weight: 600;
  }

  .row {
    display: flex;
  }
  .row--user {
    justify-content: flex-end;
  }
  .row--assistant {
    justify-content: flex-start;
  }

  .bubble {
    max-width: 86%;
    border-radius: 13px;
    padding: 11px 14px;
    font-size: 14px;
    line-height: 1.6;
    word-wrap: break-word;
    overflow-wrap: anywhere;
  }
  .bubble--user {
    background: linear-gradient(180deg, #15333a, #0f262c);
    border: 1px solid var(--cyan-dim);
    color: #d8f6f7;
    border-bottom-right-radius: 4px;
    white-space: pre-wrap;
    font-family: var(--sans);
  }
  .bubble--assistant {
    background: var(--bg-2);
    border: 1px solid var(--line-strong);
    border-bottom-left-radius: 4px;
    color: var(--ink);
  }
  /* prose renders inline so text and tool chips share the same flow */
  .prose {
    white-space: pre-wrap;
  }

  /* in-flight assistant "thinking" dots */
  .thinking,
  .working-dots {
    display: inline-flex;
    gap: 4px;
    align-items: center;
  }
  .thinking span,
  .working-dots span {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--amber);
    opacity: 0.4;
    animation: blink 1.2s infinite ease-in-out;
  }
  .thinking span:nth-child(2),
  .working-dots span:nth-child(2) {
    animation-delay: 0.18s;
  }
  .thinking span:nth-child(3),
  .working-dots span:nth-child(3) {
    animation-delay: 0.36s;
  }
  @keyframes blink {
    0%, 80%, 100% { opacity: 0.25; transform: translateY(0); }
    40% { opacity: 1; transform: translateY(-2px); }
  }

  /* turn result / error footer inside the assistant bubble */
  .turn-note {
    margin-top: 10px;
    padding: 7px 10px;
    border-radius: var(--radius-sm);
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.5;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .turn-note--ok {
    background: rgba(93, 219, 142, 0.07);
    border: 1px solid var(--green-dim);
    color: #bff5d3;
  }
  .turn-note--error {
    background: rgba(255, 77, 77, 0.08);
    border: 1px solid var(--danger-deep);
    color: #ffd5d5;
  }
  .turn-note-tag {
    text-transform: uppercase;
    letter-spacing: 0.14em;
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 4px;
    border: 1px solid currentColor;
    opacity: 0.85;
  }
  .turn-note-body {
    flex: 1;
    min-width: 0;
  }
  .turn-note-time {
    margin-left: auto;
    color: var(--ink-faint);
  }

  /* ── composer ─────────────────────────────────────────────────────────── */
  .composer {
    border-top: 1px solid var(--line);
    padding: 12px;
    background: linear-gradient(0deg, rgba(255, 255, 255, 0.012), transparent);
  }
  .working-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--amber);
    padding: 0 4px 9px;
    letter-spacing: 0.02em;
  }
  .composer-row {
    display: flex;
    gap: 10px;
    align-items: flex-end;
  }
  textarea {
    flex: 1;
    resize: none;
    max-height: 168px;
    min-height: 44px;
    background: var(--bg-2);
    color: var(--ink);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    padding: 11px 13px;
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.5;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
    field-sizing: content; /* progressive: auto-grows where supported */
  }
  textarea::placeholder {
    color: var(--ink-faint);
  }
  textarea:focus {
    border-color: var(--cyan-dim);
    box-shadow: 0 0 0 3px rgba(61, 209, 214, 0.12);
  }
  textarea:disabled {
    opacity: 0.55;
  }

  .send {
    flex: none;
    align-self: stretch;
    min-width: 78px;
    padding: 0 18px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--cyan-dim);
    background: linear-gradient(180deg, #19474b, #103539);
    color: #d8f6f7;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.04em;
    transition: filter 0.15s, border-color 0.15s, opacity 0.15s;
  }
  .send:hover:not(:disabled) {
    filter: brightness(1.22);
    border-color: var(--cyan);
  }
  .send:disabled {
    opacity: 0.4;
    background: var(--bg-2);
    border-color: var(--line-strong);
    color: var(--ink-faint);
  }
</style>
