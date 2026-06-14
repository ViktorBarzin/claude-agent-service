<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    openSession,
    attachStream,
    sendPrompt,
    cancelTurn,
    loadSessionId,
    saveSessionId,
    clearSessionId,
  } from './lib/api.js';
  import { createTranscript, reduceEvent } from './lib/transcript.js';
  import Chat from './Chat.svelte';
  import VmControls from './VmControls.svelte';

  // ── lifecycle state ───────────────────────────────────────────────────────
  // link: connecting | attached | error  (the EventSource to the session)
  let link = $state('connecting');
  let linkError = $state('');
  let sessionId = $state('');
  let caughtUp = $state(false); // replay drained → live tailing
  let turnActive = $state(false); // a turn is running (Stop shown, Send off)
  let sending = $state(false); // a prompt POST is in flight

  // The transcript is folded with a plain mutable object; we bump `rev` to
  // notify the view of in-place mutations (cheaper than cloning the whole
  // message list on every streamed token). `tx` is $state too, so REASSIGNING
  // it (reset / new session) also propagates to the Chat prop. $state.raw keeps
  // the object un-proxied so the hot per-token path stays a plain mutation.
  let tx = $state.raw(createTranscript());
  let rev = $state(0);

  let es = null; // the live EventSource

  // Mobile: VM controls live in a slide-up sheet. Desktop (≥900px): a column.
  let showControls = $state(false);

  function resetTranscript() {
    tx = createTranscript();
    rev++;
  }

  function onEvent(ev) {
    if (reduceEvent(tx, ev)) {
      // turn liveness tracks the folder's view of the stream, so a turn started
      // in ANOTHER tab (or before a reload) still flips us into "active".
      turnActive = tx.activeUserSeen;
      rev++;
    }
  }

  function closeStream() {
    if (es) {
      es.close();
      es = null;
    }
  }

  function attach(id) {
    closeStream();
    sessionId = id;
    caughtUp = false;
    link = 'connecting';
    linkError = '';
    es = attachStream(id, {
      onOpen: () => {
        // a successful (re)connection clears any prior transient error
        if (link !== 'attached') link = 'attached';
        linkError = '';
      },
      onCaughtUp: () => {
        caughtUp = true;
        link = 'attached';
      },
      onEvent,
      onError: () => {
        // EventSource auto-reconnects on a transient drop (readyState
        // CONNECTING). Only a terminal CLOSED state is a hard failure. The
        // server keeps the turn running regardless, so we surface a soft note
        // and let the browser retry.
        if (es && es.readyState === EventSource.CLOSED) {
          link = 'error';
          linkError = 'lost the connection to the session — retrying…';
          // a closed source won't retry itself; re-attach to the same id.
          setTimeout(() => {
            if (sessionId === id) attach(id);
          }, 1500);
        } else {
          link = 'connecting';
        }
      },
    });
  }

  async function bootstrap() {
    link = 'connecting';
    linkError = '';
    resetTranscript();
    const existing = loadSessionId();
    if (existing) {
      // Reuse the persisted id and attach. If it's gone (pod restart → 404 on
      // the stream), the EventSource errors; we detect the 404-shaped close and
      // mint a fresh session below.
      attach(existing);
      // Probe liveness: if the attach can't open within a grace window AND the
      // id is stale, create a new one. We rely on onError(CLOSED) for the 404.
      return;
    }
    await createFresh();
  }

  async function createFresh() {
    try {
      link = 'connecting';
      const id = await openSession();
      saveSessionId(id);
      attach(id);
    } catch (err) {
      link = 'error';
      linkError = err instanceof Error ? err.message : String(err);
    }
  }

  // "New session": archive the local id, mint a new one, re-attach.
  async function newSession() {
    if (turnActive || sending) return;
    closeStream();
    clearSessionId();
    resetTranscript();
    turnActive = false;
    await createFresh();
  }

  // Send a prompt (typed or a preset). Output arrives via the attach stream.
  async function submitPrompt(prompt) {
    const text = (prompt || '').trim();
    if (!text || turnActive || sending) return;
    if (!sessionId) {
      await createFresh();
      if (!sessionId) return;
    }
    sending = true;
    turnActive = true; // optimistic: the working indicator shows immediately
    try {
      const res = await sendPrompt({ session_id: sessionId, prompt: text });
      if (res.status === 'busy') {
        flash = 'A turn is already running.';
        // turn really is active; keep the indicator, the stream will end it.
      } else if (res.status === 'gone') {
        // session evaporated (pod restart). Re-create and resend once.
        clearSessionId();
        await createFresh();
        if (sessionId) await sendPrompt({ session_id: sessionId, prompt: text });
      }
    } catch (err) {
      flash = err instanceof Error ? err.message : String(err);
      turnActive = tx.activeUserSeen; // back off the optimistic flag on failure
    } finally {
      sending = false;
    }
  }

  async function stopTurn() {
    if (!sessionId) return;
    try {
      await cancelTurn(sessionId);
      // turn_end / cancelled events arrive via the stream and flip turnActive.
    } catch (err) {
      flash = err instanceof Error ? err.message : String(err);
    }
  }

  // a transient toast (409 / network blips), auto-cleared
  let flash = $state('');
  let flashTimer;
  $effect(() => {
    if (flash) {
      clearTimeout(flashTimer);
      flashTimer = setTimeout(() => (flash = ''), 4200);
    }
  });

  onMount(bootstrap);
  onDestroy(closeStream);

  // ── header status lamp ──────────────────────────────────────────────────
  // One quietly-living "system pulse": idle/connecting (cyan breathe),
  // working (amber pulse), error (steady red — the ONLY non-power red, used
  // sparingly for the lamp because connection loss IS the emergency here).
  const lamp = $derived(
    link === 'error'
      ? 'error'
      : turnActive
        ? 'working'
        : link === 'attached'
          ? 'live'
          : 'connecting'
  );
  const lampLabel = $derived(
    {
      error: 'link down',
      working: 'agent working',
      live: 'attached',
      connecting: 'connecting',
    }[lamp]
  );
  const shortId = $derived(sessionId ? sessionId.slice(0, 8) : '········');
</script>

<div class="shell">
  <header class="rail rise-in" style="--d:0ms">
    <div class="rail-title">
      <span class="brand-mark" aria-hidden="true">
        <!-- breakglass glyph: a wrench struck through a fracture line -->
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor"
          stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M15.5 5.5a3.6 3.6 0 0 0-4.7 4.4L4 16.7 7.3 20l6.8-6.8a3.6 3.6 0 0 0 4.4-4.7l-2.2 2.2-2.2-.6-.6-2.2 2-2.6Z" />
          <path class="frac" d="M3 3l3.2 4.1L4.4 8.6 7 12" stroke-dasharray="2 2.4" />
        </svg>
      </span>
      <h1>devvm<span class="accent"> breakglass</span></h1>
    </div>

    <div class="rail-right">
      <span class="lamp-wrap" title={lampLabel}>
        <span class="lamp lamp--{lamp}" aria-hidden="true"></span>
        <span class="lamp-text lamp-text--{lamp}">
          {#if lamp === 'error'}
            link down
          {:else if lamp === 'working'}
            working
          {:else if lamp === 'live'}
            <code class="sid">{shortId}</code>
          {:else}
            connecting
          {/if}
        </span>
      </span>

      <!-- Mobile-only: open the VM control sheet. Hidden on desktop (column). -->
      <button
        class="rail-btn rail-btn--vm"
        onclick={() => (showControls = true)}
        aria-label="Open direct VM controls"
      >
        <span class="bolt" aria-hidden="true">⚡</span><span class="rail-btn-label">VM</span>
      </button>

      <button
        class="rail-btn"
        onclick={newSession}
        disabled={turnActive || sending || link === 'connecting'}
        title={turnActive ? 'wait for the current turn to finish' : 'archive this session and start fresh'}
      >
        New
      </button>
    </div>
  </header>

  {#if link === 'error'}
    <div class="rail-note" role="alert">
      <span>{linkError || "Can't reach the breakglass backend."}</span>
      <span class="rail-note-aside">The <strong>⚡ VM</strong> power controls still work without the chat.</span>
      <button class="rail-note-retry" onclick={bootstrap}>Reconnect</button>
    </div>
  {/if}

  {#if flash}
    <div class="toast" role="status">{flash}</div>
  {/if}

  <main class="stage">
    <section class="chat-pane rise-in" style="--d:80ms" aria-label="Recovery chat">
      <Chat
        {tx}
        {rev}
        {caughtUp}
        {turnActive}
        sending={sending}
        linkState={link}
        onSubmit={submitPrompt}
        onStop={stopTurn}
      />
    </section>

    <aside
      class="controls-pane rise-in"
      class:open={showControls}
      style="--d:160ms"
      aria-label="Direct VM control"
    >
      <div class="sheet-grip" aria-hidden="true"></div>
      <div class="controls-head">
        <span class="controls-head-title">Direct VM control</span>
        <button class="sheet-close" onclick={() => (showControls = false)} aria-label="Close VM controls">✕</button>
      </div>
      <VmControls />
    </aside>
  </main>

  <button
    class="sheet-backdrop"
    class:show={showControls}
    onclick={() => (showControls = false)}
    tabindex="-1"
    aria-hidden="true"
  ></button>
</div>

<style>
  .shell {
    height: 100%;
    display: flex;
    flex-direction: column;
    max-width: 1520px;
    margin: 0 auto;
    /* honour the notch on landscape / edge-to-edge */
    padding-left: var(--safe-left);
    padding-right: var(--safe-right);
  }

  /* ── status rail ───────────────────────────────────────────────────────── */
  .rail {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: max(10px, var(--safe-top)) 14px 10px;
    border-bottom: 1px solid var(--line);
    background:
      linear-gradient(180deg, rgba(61, 209, 214, 0.03), transparent 60%),
      linear-gradient(180deg, rgba(255, 255, 255, 0.015), transparent);
    flex: none;
  }
  .rail-title {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .brand-mark {
    color: var(--cyan);
    display: inline-flex;
    filter: drop-shadow(0 0 10px rgba(61, 209, 214, 0.35));
    flex: none;
  }
  .brand-mark .frac { color: var(--amber); stroke: var(--amber); opacity: 0.85; }
  h1 {
    margin: 0;
    font-family: var(--mono);
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--ink);
    white-space: nowrap;
  }
  .accent {
    color: var(--cyan);
    text-shadow: 0 0 18px rgba(61, 209, 214, 0.4);
  }

  .rail-right {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: none;
  }

  /* the living system-pulse lamp */
  .lamp-wrap {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 0 4px;
    font-family: var(--mono);
    font-size: 12px;
  }
  .lamp {
    position: relative;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex: none;
    background: var(--ink-faint);
  }
  /* a soft halo ring that pulses outward — the "instrument is powered" tell */
  .lamp::after {
    content: '';
    position: absolute;
    inset: -4px;
    border-radius: 50%;
    border: 1px solid currentColor;
    opacity: 0;
  }
  .lamp--live {
    background: var(--cyan);
    color: var(--cyan);
    box-shadow: 0 0 10px 1px rgba(61, 209, 214, 0.65);
    animation: lamp-breathe 3.6s ease-in-out infinite;
  }
  .lamp--live::after { animation: lamp-ring 3.6s ease-out infinite; }
  .lamp--connecting {
    background: var(--cyan-dim);
    color: var(--cyan);
    animation: lamp-blink 1.4s ease-in-out infinite;
  }
  .lamp--working {
    background: var(--amber);
    color: var(--amber);
    box-shadow: 0 0 10px 1px rgba(245, 182, 87, 0.7);
    animation: lamp-pulse 1s ease-in-out infinite;
  }
  .lamp--working::after { animation: lamp-ring 1s ease-out infinite; }
  .lamp--error {
    background: var(--danger);
    color: var(--danger);
    box-shadow: 0 0 10px 1px var(--danger-glow);
    animation: lamp-pulse 1.2s ease-in-out infinite;
  }
  @keyframes lamp-breathe { 0%, 100% { opacity: 0.6; } 50% { opacity: 1; } }
  @keyframes lamp-blink { 0%, 100% { opacity: 0.35; } 50% { opacity: 0.9; } }
  @keyframes lamp-pulse {
    0%, 100% { transform: scale(0.82); opacity: 0.75; }
    50% { transform: scale(1.12); opacity: 1; }
  }
  @keyframes lamp-ring {
    0% { opacity: 0.5; transform: scale(0.6); }
    70% { opacity: 0; transform: scale(1.8); }
    100% { opacity: 0; transform: scale(1.8); }
  }
  .lamp-text {
    letter-spacing: 0.04em;
    color: var(--ink-dim);
    max-width: 88px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .lamp-text--live .sid { color: var(--cyan); letter-spacing: 0.06em; }
  .lamp-text--working { color: var(--amber); }
  .lamp-text--error { color: var(--danger-bright); }
  .lamp-text--connecting { color: var(--ink-faint); }
  .sid { font-family: var(--mono); }
  /* On the tightest phones the title + lamp text + two buttons crowd; keep the
     living dot (the system pulse) and drop the text label until there's room. */
  @media (max-width: 439px) {
    .lamp-text { display: none; }
    .lamp-wrap { padding: 0; }
  }

  /* rail buttons — touch-first (≥44px tall via padding + line height) */
  .rail-btn {
    min-height: 44px;
    padding: 0 14px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line-strong);
    background: var(--bg-2);
    color: var(--ink-dim);
    font-size: 13px;
    letter-spacing: 0.03em;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: border-color 0.15s, background 0.15s, color 0.15s;
  }
  .rail-btn:hover:not(:disabled) { border-color: var(--line-bright); color: var(--ink); }
  .rail-btn:active:not(:disabled) { background: var(--bg-3); }
  .rail-btn:disabled { opacity: 0.42; }
  .rail-btn--vm {
    border-color: var(--amber-dim);
    color: var(--amber);
  }
  .rail-btn--vm:hover:not(:disabled) { border-color: var(--amber); color: var(--amber); }
  .bolt { font-size: 13px; line-height: 1; }

  .rail-note {
    margin: 10px 12px 0;
    padding: 10px 13px;
    border: 1px solid var(--danger-deep);
    border-left-width: 3px;
    background: rgba(255, 77, 77, 0.07);
    color: #ffd9d9;
    border-radius: var(--radius-sm);
    font-size: 13px;
    line-height: 1.5;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 12px;
    flex: none;
  }
  .rail-note-aside { color: #f0b8b8; }
  .rail-note-aside strong { color: #fff; font-family: var(--mono); }
  .rail-note-retry {
    margin-left: auto;
    border: 1px solid var(--danger-deep);
    background: transparent;
    color: var(--danger-bright);
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 12px;
    min-height: 36px;
  }
  .rail-note-retry:hover { background: rgba(255, 77, 77, 0.12); }

  .toast {
    margin: 10px 12px 0;
    padding: 9px 13px;
    border: 1px solid var(--line-strong);
    border-left: 3px solid var(--amber);
    background: var(--bg-2);
    color: var(--amber);
    border-radius: var(--radius-sm);
    font-family: var(--mono);
    font-size: 12.5px;
    line-height: 1.45;
    flex: none;
    animation: rise-in 0.28s ease-out both;
  }

  /* ── stage ───────────────────────────────────────────────────────────── */
  .stage {
    flex: 1;
    min-height: 0;
    display: flex;
    min-width: 0;
    padding: 10px;
  }
  .chat-pane {
    flex: 1;
    min-height: 0;
    min-width: 0;
    display: flex;
  }

  /* ── VM controls: a slide-up bottom sheet on mobile ──────────────────── */
  .controls-pane {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 40;
    max-height: 88dvh;
    display: flex;
    flex-direction: column;
    background: var(--bg-1);
    border-top: 1px solid var(--line-strong);
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    box-shadow: var(--shadow-sheet);
    padding: 8px 14px calc(14px + var(--safe-bottom));
    transform: translateY(102%);
    transition: transform 0.3s cubic-bezier(0.32, 0.72, 0, 1);
    /* the rise-in entrance is for the desktop column; the sheet is transform-
       controlled, so cancel the shared keyframe here. */
    animation: none !important;
  }
  .controls-pane.open {
    transform: translateY(0);
  }
  .sheet-grip {
    width: 40px;
    height: 4px;
    border-radius: 99px;
    background: var(--line-bright);
    margin: 4px auto 10px;
    flex: none;
  }
  .controls-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
    flex: none;
  }
  .controls-head-title {
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: var(--amber);
  }
  .sheet-close {
    width: 40px;
    height: 40px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line-strong);
    background: var(--bg-2);
    color: var(--ink-dim);
    font-size: 14px;
  }
  .sheet-close:active { background: var(--bg-3); }

  .sheet-backdrop {
    position: fixed;
    inset: 0;
    z-index: 30;
    border: 0;
    padding: 0;
    background: rgba(2, 4, 7, 0.62);
    backdrop-filter: blur(1.5px);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.24s;
  }
  .sheet-backdrop.show {
    opacity: 1;
    pointer-events: auto;
  }

  /* ── desktop: controls become a static side column ─────────────────────── */
  @media (min-width: 900px) {
    .rail { padding: 14px 18px; }
    h1 { font-size: 19px; }
    .stage {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 384px;
      gap: 16px;
      padding: 16px 18px 18px;
    }
    .chat-pane { display: flex; }
    .rail-btn--vm { display: none; }
    .controls-pane {
      position: static;
      max-height: none;
      transform: none;
      box-shadow: none;
      border: none;
      border-radius: 0;
      padding: 0;
      z-index: auto;
      animation: rise-in 0.5s cubic-bezier(0.22, 0.61, 0.36, 1) both !important;
      animation-delay: var(--d, 0ms) !important;
    }
    .sheet-grip,
    .controls-head,
    .sheet-backdrop { display: none; }
  }
</style>
