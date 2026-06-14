<script>
  import { tick } from 'svelte';
  import ToolChip from './ToolChip.svelte';

  let {
    tx, // the folded transcript state (plain object, see lib/transcript.js)
    rev = 0, // bumped on every in-place mutation to retrigger reactivity
    caughtUp = false, // replay drained → staggered reveal may run
    turnActive = false, // a turn is running: show Stop, hide Send
    sending = false, // a prompt POST is in flight (brief)
    linkState = 'connecting', // connecting | attached | error
    onSubmit = (/** @type {string} */ _p) => {},
    onStop = () => {},
  } = $props();

  // The five quick-action presets — the mobile win: one tap, no typing.
  const PRESETS = [
    {
      label: 'Triage',
      icon: '◑',
      prompt:
        'Triage the devvm: uptime, load, memory, swap, disk usage, failed systemd units, and the last 30 lines of dmesg. Summarize what\'s wrong.',
    },
    {
      label: 'Memory / OOM',
      icon: '▦',
      prompt:
        'Check devvm memory pressure: free -h, top memory consumers, any recent OOM-kills in dmesg/journal, and swap usage. Is it OOMing?',
    },
    {
      label: 'Disk',
      icon: '▤',
      prompt:
        'What\'s filling the devvm disk? df -h, then the biggest directories/files under the fullest mount. Anything safe to clear?',
    },
    {
      label: 'Services',
      icon: '⚙',
      prompt:
        'List failed or stuck systemd units on the devvm (systemctl --failed) and show the status + recent journal lines for any that are down.',
    },
    {
      label: 'QEMU wedged?',
      icon: '◫',
      prompt:
        'Is the devvm\'s QEMU wedged (I/O stall)? Check guest responsiveness over SSH, then ssh pve forensics for VM 102\'s qm status/QMP/guest-agent. Tell me if a cycle is needed.',
    },
  ];

  let draft = $state('');
  let scroller;
  let inputEl;
  let pinnedToBottom = true;

  // re-derive the message list whenever the folder mutates (rev bump). The
  // transcript is folded with in-place mutation on a $state.raw object, so no
  // reference changes on its own — we depend on `rev` explicitly and rebuild
  // fresh objects (message + its parts array) so Svelte's keyed {#each} re-
  // renders streamed prose/chips on every token. Transcripts are small; the
  // per-token copy is cheap and keeps the hot streaming path bug-free.
  const messages = $derived(
    rev >= 0 && tx
      ? tx.messages.map((m) =>
          m.role === 'assistant' ? { ...m, parts: m.parts.slice() } : { ...m }
        )
      : []
  );
  const isEmpty = $derived(messages.length === 0);
  const canSend = $derived(linkState !== 'error' && !turnActive && draft.trim().length > 0);
  const inputReady = $derived(!turnActive);

  // ── auto-scroll (only while pinned to the bottom) ─────────────────────────
  function onScroll() {
    if (!scroller) return;
    const gap = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    pinnedToBottom = gap < 64;
  }
  async function scrollToBottom(force = false) {
    if (!force && !pinnedToBottom) return;
    await tick();
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }
  // any transcript change → keep the view pinned if the user is at the bottom
  $effect(() => {
    rev; // track
    scrollToBottom();
  });

  function fire(prompt) {
    if (turnActive) return;
    pinnedToBottom = true;
    onSubmit(prompt);
    scrollToBottom(true);
  }

  function send() {
    const text = draft.trim();
    if (!text || turnActive) return;
    draft = '';
    fire(text);
    // restore single-row height after clearing
    tick().then(() => inputEl?.focus());
  }

  function onKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
    // Shift+Enter → newline (default behaviour)
  }

  function fmtDuration(ms) {
    if (ms == null) return '';
    if (ms < 1000) return `${ms} ms`;
    return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
  }

  // a freshly-attached transcript reveals with a brief stagger; cap the delay
  // so a long replay doesn't animate forever.
  function revealDelay(i) {
    if (!caughtUp) return 0;
    return Math.min(i, 6) * 45;
  }
</script>

<div class="chat">
  <div class="chat-head">
    <span class="chat-head-label">Recovery agent</span>
    <span class="chat-head-hint">SSHes into the devvm to diagnose &amp; repair</span>
  </div>

  <div class="stream" bind:this={scroller} onscroll={onScroll}>
    {#if isEmpty}
      <div class="empty" class:dim={linkState === 'connecting'}>
        <div class="empty-mark" aria-hidden="true">⌁</div>
        <p class="empty-title">
          {#if linkState === 'error'}
            The agent is unreachable.
          {:else if linkState === 'connecting'}
            Attaching to the session…
          {:else}
            The agent is standing by.
          {/if}
        </p>
        <p class="empty-sub">
          {#if linkState === 'error'}
            The cluster or network may be down. You can still power-cycle the VM
            with <strong>⚡ Direct VM control</strong> — it needs no agent.
          {:else}
            Tap a preset below or describe the symptom — "devvm unreachable",
            "disk full", "ssh hangs" — and it will connect over SSH, investigate,
            and stream its work here. For a hard power action, use
            <strong>⚡ Direct VM control</strong>.
          {/if}
        </p>
      </div>
    {/if}

    {#each messages as msg (msg.key)}
      {#if msg.role === 'user'}
        <div class="row row--user rise-in" style="--d:{revealDelay(0)}ms">
          <div class="bubble bubble--user">{msg.text}</div>
        </div>
      {:else}
        <div class="row row--assistant rise-in" style="--d:{revealDelay(0)}ms">
          <div class="bubble bubble--assistant">
            {#if msg.parts.length === 0 && !msg.result && !msg.error && !msg.cancelled}
              <span class="thinking" aria-label="working">
                <span></span><span></span><span></span>
              </span>
            {/if}
            {#each msg.parts as part, j (j)}
              {#if part.type === 'text'}<span class="prose">{part.text}</span>{:else}<ToolChip name={part.name} command={part.command} />{/if}
            {/each}

            {#if msg.error}
              <div class="turn-note turn-note--error">
                <span class="turn-note-tag">error</span>
                <span class="turn-note-body">{msg.error}</span>
              </div>
            {:else if msg.cancelled}
              <div class="turn-note turn-note--muted">
                <span class="turn-note-tag">stopped</span>
                <span class="turn-note-body">turn cancelled</span>
              </div>
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

  <div class="dock">
    <!-- quick-action preset bar: horizontally scrollable, one-tap prompts -->
    <div class="presets" role="group" aria-label="Quick actions">
      {#each PRESETS as p (p.label)}
        <button
          class="preset"
          onclick={() => fire(p.prompt)}
          disabled={turnActive || linkState === 'error'}
          title={p.prompt}
        >
          <span class="preset-icon" aria-hidden="true">{p.icon}</span>
          <span class="preset-label">{p.label}</span>
        </button>
      {/each}
    </div>

    <form
      class="composer"
      onsubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      {#if turnActive}
        <div class="working-bar" aria-live="polite">
          <span class="working-dots"><span></span><span></span><span></span></span>
          <span>agent working — streaming live</span>
        </div>
      {/if}
      <div class="composer-row">
        <textarea
          bind:this={inputEl}
          bind:value={draft}
          onkeydown={onKeydown}
          placeholder={inputReady
            ? 'Describe the problem…  (Enter to send · Shift+Enter for a new line)'
            : 'A turn is running — Stop it to type, or wait…'}
          rows="1"
          disabled={!inputReady}
          spellcheck="false"
          enterkeyhint="send"
        ></textarea>
        {#if turnActive}
          <button type="button" class="stop" onclick={onStop} title="Stop the running turn">
            <span class="stop-glyph" aria-hidden="true"></span>
            Stop
          </button>
        {:else}
          <button type="submit" class="send" disabled={!canSend}>
            {sending ? '···' : 'Send'}
          </button>
        {/if}
      </div>
    </form>
  </div>
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
    padding: 12px 18px;
    border-bottom: 1px solid var(--line);
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.018), transparent);
    flex: none;
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
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .stream {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 20px 16px 10px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    scroll-behavior: smooth;
  }

  /* empty state */
  .empty {
    margin: auto;
    max-width: 470px;
    text-align: center;
    padding: 24px 14px;
    color: var(--ink-dim);
  }
  .empty.dim { opacity: 0.8; }
  .empty-mark {
    font-size: 42px;
    color: var(--cyan-dim);
    line-height: 1;
    margin-bottom: 14px;
    text-shadow: 0 0 26px rgba(61, 209, 214, 0.3);
    animation: lamp-breathe 3.6s ease-in-out infinite;
  }
  @keyframes lamp-breathe { 0%, 100% { opacity: 0.7; } 50% { opacity: 1; } }
  .empty-title {
    font-family: var(--mono);
    color: var(--ink);
    font-size: 15px;
    margin: 0 0 8px;
    letter-spacing: 0.01em;
  }
  .empty-sub {
    font-size: 13px;
    line-height: 1.6;
    color: var(--ink-faint);
    margin: 0;
  }
  .empty-sub strong { color: var(--ink-dim); font-weight: 600; }

  .row { display: flex; }
  .row--user { justify-content: flex-end; }
  .row--assistant { justify-content: flex-start; }

  .bubble {
    max-width: 88%;
    border-radius: 13px;
    padding: 11px 14px;
    font-size: 14px;
    line-height: 1.62;
    word-wrap: break-word;
    overflow-wrap: anywhere;
  }
  .bubble--user {
    background: linear-gradient(180deg, #123036, #0d2329);
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
  .prose { white-space: pre-wrap; }

  /* in-flight "thinking" dots */
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
  .working-dots span:nth-child(2) { animation-delay: 0.18s; }
  .thinking span:nth-child(3),
  .working-dots span:nth-child(3) { animation-delay: 0.36s; }
  @keyframes blink {
    0%, 80%, 100% { opacity: 0.25; transform: translateY(0); }
    40% { opacity: 1; transform: translateY(-2px); }
  }

  /* turn result / error / stopped footer inside the assistant bubble */
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
    /* the error tint here is amber-leaning text on a faint warm wash, NOT the
       reserved power-action red — a turn error is not a destructive action. */
    background: rgba(245, 182, 87, 0.06);
    border: 1px solid var(--amber-dim);
    color: #f7d49a;
  }
  .turn-note--muted {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--line-strong);
    color: var(--ink-faint);
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
  .turn-note-body { flex: 1; min-width: 0; }
  .turn-note-time { margin-left: auto; color: var(--ink-faint); }

  /* ── dock: presets + composer, pinned to the bottom ────────────────────── */
  .dock {
    flex: none;
    border-top: 1px solid var(--line);
    background: linear-gradient(0deg, rgba(255, 255, 255, 0.015), transparent);
  }

  .presets {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding: 11px 12px 4px;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
    /* fade the right edge to hint there's more to scroll */
    mask-image: linear-gradient(90deg, transparent 0, #000 14px, #000 calc(100% - 18px), transparent 100%);
  }
  .presets::-webkit-scrollbar { display: none; }
  .preset {
    flex: none;
    min-height: 38px;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 0 13px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    background: var(--bg-2);
    color: var(--ink-dim);
    font-family: var(--mono);
    font-size: 12.5px;
    letter-spacing: 0.02em;
    white-space: nowrap;
    transition: border-color 0.15s, color 0.15s, background 0.15s, transform 0.06s;
  }
  .preset:hover:not(:disabled) {
    border-color: var(--cyan-dim);
    color: var(--ink);
    background: var(--bg-3);
  }
  .preset:active:not(:disabled) { transform: translateY(1px); }
  .preset:disabled { opacity: 0.4; }
  .preset-icon { color: var(--cyan); font-size: 12px; }

  .composer {
    padding: 8px 12px calc(12px + var(--safe-bottom));
  }
  .working-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--amber);
    padding: 2px 4px 9px;
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
    max-height: 160px;
    min-height: 48px;
    background: var(--bg-2);
    color: var(--ink);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    padding: 13px 13px;
    font-family: var(--sans);
    /* 16px: anything smaller makes iOS Safari auto-zoom on focus (mobile is the
       primary client) — the zoom then shifts the composer out of view. */
    font-size: 16px;
    line-height: 1.5;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
    field-sizing: content; /* progressive: auto-grows where supported */
  }
  textarea::placeholder { color: var(--ink-faint); }
  textarea:focus {
    border-color: var(--cyan-dim);
    box-shadow: 0 0 0 3px rgba(61, 209, 214, 0.12);
  }
  textarea:disabled { opacity: 0.55; }

  .send,
  .stop {
    flex: none;
    align-self: stretch;
    min-width: 82px;
    min-height: 48px;
    padding: 0 18px;
    border-radius: var(--radius-sm);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.05em;
    transition: filter 0.15s, border-color 0.15s, opacity 0.15s, background 0.15s;
  }
  .send {
    border: 1px solid var(--cyan-dim);
    background: linear-gradient(180deg, #16464a, #0e3438);
    color: #d8f6f7;
  }
  .send:hover:not(:disabled) { filter: brightness(1.24); border-color: var(--cyan); }
  .send:disabled {
    opacity: 0.4;
    background: var(--bg-2);
    border-color: var(--line-strong);
    color: var(--ink-faint);
  }
  /* Stop is NOT red — red is reserved for destructive VM power. Stop is a calm
     neutral control with a square "halt" glyph. */
  .stop {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border: 1px solid var(--line-bright);
    background: var(--bg-3);
    color: var(--ink);
  }
  .stop:hover { border-color: var(--ink-faint); filter: brightness(1.1); }
  .stop-glyph {
    width: 10px;
    height: 10px;
    border-radius: 2px;
    background: var(--amber);
    box-shadow: 0 0 8px rgba(245, 182, 87, 0.55);
    animation: lamp-pulse 1s ease-in-out infinite;
  }
  @keyframes lamp-pulse {
    0%, 100% { transform: scale(0.85); opacity: 0.8; }
    50% { transform: scale(1.08); opacity: 1; }
  }
</style>
