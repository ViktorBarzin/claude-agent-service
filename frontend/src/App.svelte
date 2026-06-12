<script>
  import { onMount } from 'svelte';
  import { openSession } from './lib/api.js';
  import Chat from './Chat.svelte';
  import VmControls from './VmControls.svelte';

  // ── session lifecycle ────────────────────────────────────────────────────
  let sessionId = $state('');
  let sessionState = $state('connecting'); // connecting | ready | error
  let sessionError = $state('');
  let streaming = $state(false);

  // Mobile: the VM controls live in a slide-up sheet. Desktop: a side column
  // (CSS hides the toggle and pins the sheet open as a column ≥900px).
  let showControls = $state(false);

  async function newSession() {
    sessionState = 'connecting';
    sessionError = '';
    try {
      sessionId = await openSession();
      sessionState = 'ready';
    } catch (err) {
      sessionState = 'error';
      sessionError = err instanceof Error ? err.message : String(err);
    }
  }

  onMount(newSession);

  function onLiveSession(id) {
    if (id) sessionId = id;
  }

  const shortId = $derived(sessionId ? sessionId.slice(0, 8) : '────────');
  const dotState = $derived(
    sessionState === 'error' ? 'error' : streaming ? 'busy' : sessionState === 'ready' ? 'ready' : 'idle'
  );
</script>

<div class="shell">
  <header class="rail">
    <div class="rail-title">
      <span class="glyph" aria-hidden="true">🔧</span>
      <h1>devvm <span class="accent">breakglass</span></h1>
    </div>

    <div class="rail-right">
      <span class="rail-status">
        <span class="dot dot--{dotState}" aria-hidden="true"></span>
        {#if sessionState === 'error'}
          <span class="session-bad">offline</span>
        {:else if sessionState === 'connecting'}
          <span class="session-meta">connecting…</span>
        {:else}
          <code class="session-id" title={sessionId}>{shortId}</code>
        {/if}
      </span>

      <!-- Mobile-only: open the VM control sheet. Hidden on desktop (column). -->
      <button
        class="controls-toggle"
        onclick={() => (showControls = true)}
        aria-label="Open direct VM controls"
      >
        ⚡ <span class="controls-toggle-label">VM</span>
      </button>

      <button
        class="new-session"
        onclick={newSession}
        disabled={streaming || sessionState === 'connecting'}
        title={streaming ? 'wait for the current turn to finish' : 'start a fresh session'}
      >
        New
      </button>
    </div>
  </header>

  {#if sessionState === 'error'}
    <div class="rail-error" role="alert">
      Can't reach the breakglass backend — {sessionError}. The cluster or network
      may be down. The <strong>⚡ VM</strong> power controls still work without the chat.
    </div>
  {/if}

  <main class="stage">
    <section class="chat-pane" aria-label="Recovery chat">
      <Chat
        {sessionId}
        sessionReady={sessionState === 'ready'}
        {onLiveSession}
        onStreamingChange={(v) => (streaming = v)}
      />
    </section>

    <aside class="controls-pane" class:open={showControls} aria-label="Direct VM control">
      <div class="sheet-grip" aria-hidden="true"></div>
      <div class="controls-head">
        <span class="controls-head-title">Direct VM control</span>
        <button class="sheet-close" onclick={() => (showControls = false)} aria-label="Close VM controls">✕</button>
      </div>
      <VmControls />
    </aside>
  </main>

  <!-- backdrop behind the mobile sheet -->
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
    max-width: 1500px;
    margin: 0 auto;
  }

  /* ── status rail (compact, single row on mobile) ─────────────────────── */
  .rail {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
    flex: none;
  }
  .rail-title {
    display: flex;
    align-items: baseline;
    gap: 9px;
    min-width: 0;
  }
  .glyph {
    font-size: 17px;
    transform: translateY(2px);
    filter: saturate(0.85);
  }
  h1 {
    margin: 0;
    font-family: var(--mono);
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--ink);
    white-space: nowrap;
  }
  .accent {
    color: var(--cyan);
    text-shadow: 0 0 18px rgba(61, 209, 214, 0.35);
  }

  .rail-right {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: none;
  }
  .rail-status {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-family: var(--mono);
    font-size: 12px;
  }
  .session-id {
    color: var(--cyan);
    letter-spacing: 0.04em;
  }
  .session-meta {
    color: var(--amber);
  }
  .session-bad {
    color: var(--danger-bright);
  }

  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
    background: var(--ink-faint);
  }
  .dot--ready {
    background: var(--cyan);
    box-shadow: 0 0 10px 1px rgba(61, 209, 214, 0.6);
    animation: breathe 3.4s ease-in-out infinite;
  }
  .dot--busy {
    background: var(--amber);
    box-shadow: 0 0 10px 1px rgba(245, 182, 87, 0.7);
    animation: pulse 1s ease-in-out infinite;
  }
  .dot--error {
    background: var(--danger);
    box-shadow: 0 0 10px 1px var(--danger-glow);
  }
  @keyframes breathe { 0%, 100% { opacity: 0.55; } 50% { opacity: 1; } }
  @keyframes pulse {
    0%, 100% { transform: scale(0.82); opacity: 0.7; }
    50% { transform: scale(1.15); opacity: 1; }
  }

  /* touch-friendly buttons */
  .controls-toggle,
  .new-session {
    min-height: 40px;
    padding: 0 13px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line-strong);
    background: var(--bg-2);
    color: var(--ink-dim);
    font-size: 13px;
    letter-spacing: 0.02em;
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .controls-toggle {
    border-color: #5a4a2a;
    color: var(--amber);
  }
  .controls-toggle:active,
  .new-session:active {
    background: var(--bg-3);
  }
  .new-session:disabled {
    opacity: 0.45;
  }

  .rail-error {
    margin: 10px 12px 0;
    padding: 11px 14px;
    border: 1px solid var(--danger-deep);
    border-left-width: 3px;
    background: rgba(255, 77, 77, 0.07);
    color: #ffd5d5;
    border-radius: var(--radius-sm);
    font-size: 13px;
    line-height: 1.5;
    flex: none;
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
    max-height: 86dvh;
    overflow-y: auto;
    background: var(--bg-1);
    border-top: 1px solid var(--line-strong);
    border-radius: 16px 16px 0 0;
    box-shadow: 0 -18px 40px rgba(0, 0, 0, 0.55);
    padding: 8px 14px calc(14px + env(safe-area-inset-bottom));
    transform: translateY(101%);
    transition: transform 0.26s cubic-bezier(0.32, 0.72, 0, 1);
  }
  .controls-pane.open {
    transform: translateY(0);
  }
  .sheet-grip {
    width: 38px;
    height: 4px;
    border-radius: 99px;
    background: var(--line-strong);
    margin: 4px auto 10px;
  }
  .controls-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
  }
  .controls-head-title {
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: var(--amber);
  }
  .sheet-close {
    width: 34px;
    height: 34px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line-strong);
    background: var(--bg-2);
    color: var(--ink-dim);
    font-size: 14px;
  }

  .sheet-backdrop {
    position: fixed;
    inset: 0;
    z-index: 30;
    border: 0;
    padding: 0;
    background: rgba(0, 0, 0, 0.55);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.22s;
  }
  .sheet-backdrop.show {
    opacity: 1;
    pointer-events: auto;
  }

  /* ── desktop: controls become a static side column, sheet chrome gone ── */
  @media (min-width: 900px) {
    .rail {
      padding: 14px 18px;
    }
    h1 { font-size: 19px; }
    .stage {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 372px;
      gap: 16px;
      padding: 16px 18px 18px;
    }
    .chat-pane { display: flex; }
    .controls-toggle { display: none; }
    .controls-pane {
      position: static;
      max-height: none;
      overflow: visible;
      transform: none;
      box-shadow: none;
      border: none;
      border-radius: 0;
      padding: 0;
      z-index: auto;
    }
    .sheet-grip,
    .controls-head,
    .sheet-backdrop { display: none; }
  }
</style>
