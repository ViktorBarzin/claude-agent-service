<script>
  import { onMount } from 'svelte';
  import { openSession } from './lib/api.js';
  import Chat from './Chat.svelte';
  import VmControls from './VmControls.svelte';

  // ── session lifecycle ────────────────────────────────────────────────────
  // sessionId is the id we POST with. The backend also reports an authoritative
  // id in the first {kind:"session"} frame of a turn; Chat bubbles that up so
  // the rail always shows what the agent is actually resuming.
  let sessionId = $state('');
  let sessionState = $state('connecting'); // connecting | ready | error
  let sessionError = $state('');
  let streaming = $state(false); // a chat turn is in flight (drives the rail dot)

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

  // Chat reports the live session id from the stream's session frame.
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
      <span class="rail-tag">emergency recovery</span>
    </div>

    <div class="rail-status">
      <span class="dot dot--{dotState}" aria-hidden="true"></span>
      <span class="rail-session">
        {#if sessionState === 'error'}
          <span class="session-bad">session unavailable</span>
        {:else if sessionState === 'connecting'}
          <span class="session-meta">opening session…</span>
        {:else}
          <span class="session-label">session</span>
          <code class="session-id" title={sessionId}>{shortId}</code>
          {#if streaming}<span class="session-meta">· agent working</span>{/if}
        {/if}
      </span>
      <button
        class="new-session"
        onclick={newSession}
        disabled={streaming || sessionState === 'connecting'}
        title={streaming ? 'wait for the current turn to finish' : 'start a fresh session'}
      >
        New session
      </button>
    </div>
  </header>

  {#if sessionState === 'error'}
    <div class="rail-error" role="alert">
      Could not reach the breakglass backend — {sessionError}. The cluster or
      network may be down. The manual VM controls below still work independently
      of the chat agent.
    </div>
  {/if}

  <main class="grid">
    <section class="col col--chat" aria-label="Recovery chat">
      <Chat
        {sessionId}
        sessionReady={sessionState === 'ready'}
        onLiveSession={onLiveSession}
        onStreamingChange={(v) => (streaming = v)}
      />
    </section>

    <aside class="col col--controls" aria-label="Direct VM control">
      <VmControls />
    </aside>
  </main>
</div>

<style>
  .shell {
    height: 100%;
    display: flex;
    flex-direction: column;
    max-width: 1500px;
    margin: 0 auto;
    padding: 0 18px 18px;
  }

  /* ── status rail ─────────────────────────────────────────────────────── */
  .rail {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    padding: 16px 4px 14px;
    border-bottom: 1px solid var(--line);
  }

  .rail-title {
    display: flex;
    align-items: baseline;
    gap: 12px;
  }
  .glyph {
    font-size: 19px;
    transform: translateY(2px);
    filter: saturate(0.85);
  }
  h1 {
    margin: 0;
    font-family: var(--mono);
    font-size: 19px;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--ink);
  }
  .accent {
    color: var(--cyan);
    text-shadow: 0 0 18px rgba(61, 209, 214, 0.35);
  }
  .rail-tag {
    font-family: var(--mono);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    color: var(--ink-faint);
    border: 1px solid var(--line-strong);
    border-radius: 999px;
    padding: 3px 9px;
  }

  .rail-status {
    display: flex;
    align-items: center;
    gap: 14px;
    font-family: var(--mono);
    font-size: 13px;
  }
  .rail-session {
    display: inline-flex;
    align-items: baseline;
    gap: 7px;
    white-space: nowrap;
  }
  .session-label {
    color: var(--ink-faint);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
  }
  .session-id {
    color: var(--cyan);
    font-family: var(--mono);
    letter-spacing: 0.04em;
  }
  .session-meta {
    color: var(--amber);
    font-size: 12px;
  }
  .session-bad {
    color: var(--danger-bright);
  }

  /* connection lamp */
  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
    background: var(--ink-faint);
    box-shadow: 0 0 0 0 transparent;
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
  @keyframes breathe {
    0%, 100% { opacity: 0.55; }
    50% { opacity: 1; }
  }
  @keyframes pulse {
    0%, 100% { transform: scale(0.82); opacity: 0.7; }
    50% { transform: scale(1.15); opacity: 1; }
  }

  .new-session {
    background: var(--bg-2);
    color: var(--ink-dim);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    padding: 7px 13px;
    font-size: 12px;
    letter-spacing: 0.02em;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
  }
  .new-session:hover:not(:disabled) {
    border-color: var(--cyan-dim);
    color: var(--ink);
    background: var(--bg-3);
  }
  .new-session:disabled {
    opacity: 0.45;
  }

  .rail-error {
    margin: 12px 0 0;
    padding: 11px 14px;
    border: 1px solid var(--danger-deep);
    border-left-width: 3px;
    background: rgba(255, 77, 77, 0.07);
    color: #ffd5d5;
    border-radius: var(--radius-sm);
    font-size: 13px;
    line-height: 1.5;
  }

  /* ── layout ──────────────────────────────────────────────────────────── */
  .grid {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) 376px;
    gap: 18px;
    padding-top: 16px;
  }
  .col {
    min-height: 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  @media (max-width: 940px) {
    .grid {
      grid-template-columns: 1fr;
      grid-auto-rows: minmax(0, auto);
      overflow: auto;
    }
    .col--chat {
      min-height: 60vh;
    }
  }
</style>
