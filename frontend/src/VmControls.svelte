<script>
  import { onMount } from 'svelte';
  import { fetchVerbs, runVerb } from './lib/api.js';

  // ── verb catalogue ──────────────────────────────────────────────────────
  // The server is the source of truth for which verbs exist and which mutate.
  // We layer presentation metadata (label, blurb) on top, preserving a sensible
  // operator order: read-only first, then the escalating power actions.
  const META = {
    status: { label: 'status', blurb: 'qm status — is the VM up?' },
    forensics: { label: 'forensics', blurb: 'capture live diagnostic state' },
    start: { label: 'start', blurb: 'power on a stopped VM' },
    stop: { label: 'stop', blurb: 'hard power-off (pulls the plug)' },
    reset: { label: 'reset', blurb: 'warm reboot — reuses the QEMU process' },
    cycle: {
      label: 'cycle',
      blurb: 'stop → start; applies staged config; fixes a wedged QEMU',
      headline: true,
    },
  };
  const ORDER = ['status', 'forensics', 'start', 'stop', 'reset', 'cycle'];

  let loadState = $state('loading'); // loading | ready | error
  let loadError = $state('');
  let verbs = $state([]); // [{name, mutating, ...meta}]

  let confirming = $state(''); // verb awaiting confirmation, or ''
  let running = $state(''); // verb currently in flight, or ''
  let output = $state(null); // { verb, exit_code, stdout, stderr, rejected }
  let actionError = $state(''); // transport-level failure (backend unreachable)

  const busy = $derived(running !== '');

  onMount(async () => {
    try {
      const { verbs: names, mutating } = await fetchVerbs();
      const mut = new Set(mutating);
      const known = names.filter((n) => META[n]);
      const ordered = [
        ...ORDER.filter((n) => known.includes(n)),
        ...known.filter((n) => !ORDER.includes(n)),
      ];
      verbs = ordered.map((name) => ({
        name,
        mutating: mut.has(name),
        ...META[name],
      }));
      loadState = 'ready';
    } catch (err) {
      loadState = 'error';
      loadError = err instanceof Error ? err.message : String(err);
    }
  });

  const nonMutating = $derived(verbs.filter((v) => !v.mutating));
  const mutating = $derived(verbs.filter((v) => v.mutating));

  function clickVerb(v) {
    if (busy) return;
    if (v.mutating) {
      confirming = confirming === v.name ? '' : v.name; // toggle the inline confirm
    } else {
      execute(v.name);
    }
  }

  function cancelConfirm() {
    confirming = '';
  }

  async function execute(verb) {
    confirming = '';
    actionError = '';
    output = null;
    running = verb;
    try {
      output = await runVerb(verb);
    } catch (err) {
      actionError = err instanceof Error ? err.message : String(err);
    } finally {
      running = '';
    }
  }

  // styling helpers for the output panel
  const outputFailed = $derived(
    !!output && (output.rejected || (output.exit_code != null && output.exit_code !== 0))
  );
</script>

<div class="panel">
  <div class="panel-head">
    <div class="panel-head-row">
      <span class="hazard" aria-hidden="true">⚠</span>
      <h2>Direct VM control</h2>
    </div>
    <p class="panel-sub">No AI in the path — these reach the Proxmox host over a
      forced-command SSH key and work even when the agent is down.</p>
  </div>

  {#if loadState === 'loading'}
    <div class="loading">Loading controls…</div>
  {:else if loadState === 'error'}
    <div class="block-error" role="alert">
      Couldn't load the VM controls — {loadError}.
      <button class="retry" onclick={() => location.reload()}>Reload</button>
    </div>
  {:else}
    <!-- read-only actions (foldable) -->
    <details class="group" open>
      <summary class="group-label">Inspect <span class="group-tag">read-only</span></summary>
      <div class="btn-row">
        {#each nonMutating as v (v.name)}
          <button
            class="vbtn vbtn--safe"
            onclick={() => clickVerb(v)}
            disabled={busy}
            title={v.blurb}
          >
            {#if running === v.name}<span class="spin" aria-hidden="true"></span>{/if}
            <span class="vbtn-label">{v.label}</span>
          </button>
        {/each}
      </div>
    </details>

    <!-- mutating / power actions (foldable) -->
    <details class="group" open>
      <summary class="group-label group-label--danger">
        Power <span class="group-tag group-tag--danger">affects the running VM</span>
      </summary>
      <div class="danger-list">
        {#each mutating as v (v.name)}
          <div class="danger-item {v.headline ? 'danger-item--headline' : ''}">
            <button
              class="vbtn vbtn--danger {v.headline ? 'vbtn--headline' : ''}"
              onclick={() => clickVerb(v)}
              disabled={busy}
              aria-expanded={confirming === v.name}
            >
              {#if running === v.name}<span class="spin spin--danger" aria-hidden="true"></span>{/if}
              <span class="vbtn-label">{v.label}</span>
              {#if v.headline}<span class="headline-badge">recovery</span>{/if}
            </button>
            <p class="danger-blurb">{v.blurb}</p>

            {#if confirming === v.name}
              <div class="confirm" role="alertdialog" aria-label="Confirm {v.name}">
                <span class="confirm-text">
                  Confirm <strong>{v.name}</strong>? This will affect the running VM
                </span>
                <div class="confirm-actions">
                  <button class="confirm-yes" onclick={() => execute(v.name)} disabled={busy}>
                    Confirm
                  </button>
                  <button class="confirm-no" onclick={cancelConfirm} disabled={busy}>
                    Cancel
                  </button>
                </div>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    </details>

    <!-- output (foldable; a long forensics dump scrolls inside a capped box) -->
    {#if actionError}
      <div class="block-error" role="alert">
        ⚠ Command failed to reach the host — {actionError}
      </div>
    {/if}

    {#if output}
      <details class="out {outputFailed ? 'out--fail' : 'out--ok'}" open>
        <summary class="out-head">
          <code class="out-verb">{output.verb}</code>
          {#if output.rejected}
            <span class="out-status out-status--fail">rejected</span>
          {:else}
            <span class="out-status {outputFailed ? 'out-status--fail' : 'out-status--ok'}">
              exit {output.exit_code}
            </span>
          {/if}
        </summary>
        {#if output.stdout}
          <pre class="out-pre">{output.stdout}</pre>
        {/if}
        {#if output.stderr}
          <div class="out-stderr-label">stderr</div>
          <pre class="out-pre out-pre--stderr">{output.stderr}</pre>
        {/if}
        {#if !output.stdout && !output.stderr}
          <pre class="out-pre out-pre--empty">(no output)</pre>
        {/if}
      </details>
    {/if}
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    background: var(--bg-1);
    border: 1px solid var(--line);
    /* a faint danger seam down the right edge marks this as the hot zone */
    border-top: 2px solid var(--danger-deep);
    border-radius: var(--radius);
    box-shadow: var(--shadow-panel);
    overflow-y: auto;
  }

  .panel-head {
    padding: 14px 16px 12px;
    border-bottom: 1px solid var(--line);
  }
  .panel-head-row {
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .hazard {
    color: var(--danger);
    font-size: 15px;
    filter: drop-shadow(0 0 8px var(--danger-glow));
  }
  h2 {
    margin: 0;
    font-family: var(--mono);
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--ink);
  }
  .panel-sub {
    margin: 9px 0 0;
    font-size: 11.5px;
    line-height: 1.55;
    color: var(--ink-faint);
  }

  .loading {
    padding: 22px 16px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-faint);
  }

  .group {
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
  }
  .group-label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    color: var(--ink-faint);
    margin-bottom: 11px;
  }
  .group-label--danger {
    color: var(--danger-bright);
  }
  .group-tag {
    font-size: 9.5px;
    letter-spacing: 0.1em;
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid var(--line-strong);
    color: var(--ink-faint);
  }
  .group-tag--danger {
    border-color: var(--danger-deep);
    color: var(--danger-bright);
    background: rgba(255, 77, 77, 0.06);
  }

  .btn-row {
    display: flex;
    flex-wrap: wrap;
    gap: 9px;
  }

  /* shared button shape */
  .vbtn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-height: 44px; /* touch target */
    padding: 10px 16px;
    border-radius: var(--radius-sm);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: lowercase;
    transition: filter 0.14s, border-color 0.14s, background 0.14s, transform 0.06s;
  }
  .vbtn:active:not(:disabled) {
    transform: translateY(1px);
  }
  .vbtn:disabled {
    opacity: 0.4;
  }
  .vbtn-label {
    line-height: 1;
  }

  .vbtn--safe {
    background: var(--bg-2);
    color: var(--ink);
    border: 1px solid var(--line-strong);
  }
  .vbtn--safe:hover:not(:disabled) {
    border-color: var(--cyan-dim);
    background: var(--bg-3);
  }

  /* danger actions read as hot the moment you look at them */
  .danger-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .danger-item {
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
  }
  .danger-item--headline {
    padding: 11px;
    border-color: var(--danger-deep);
    background: rgba(255, 77, 77, 0.045);
  }
  .vbtn--danger {
    width: 100%;
    background: linear-gradient(180deg, rgba(255, 77, 77, 0.16), rgba(255, 77, 77, 0.07));
    color: var(--danger-bright);
    border: 1px solid var(--danger-deep);
    /* hazard stripe down the leading edge */
    border-left: 3px solid var(--danger);
    text-shadow: 0 0 12px var(--danger-glow);
  }
  .vbtn--danger:hover:not(:disabled) {
    background: linear-gradient(180deg, var(--danger), var(--danger-bright));
    color: #1a0606;
    border-color: var(--danger-bright);
    text-shadow: none;
    filter: drop-shadow(0 4px 14px var(--danger-glow));
  }
  .vbtn--headline {
    padding: 12px 15px;
    font-size: 14px;
  }
  .headline-badge {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    padding: 2px 7px;
    border-radius: 999px;
    background: var(--danger);
    color: #1a0606;
    font-weight: 700;
  }
  .danger-blurb {
    margin: 7px 2px 0;
    font-size: 11.5px;
    line-height: 1.5;
    color: var(--ink-faint);
  }
  .danger-item--headline .danger-blurb {
    color: #f0b0b0;
  }

  /* inline confirm step */
  .confirm {
    margin-top: 10px;
    padding: 11px 12px;
    border: 1px solid var(--danger);
    border-radius: var(--radius-sm);
    background: rgba(255, 77, 77, 0.1);
    animation: confirm-in 0.16s ease-out;
  }
  @keyframes confirm-in {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .confirm-text {
    display: block;
    font-size: 12.5px;
    line-height: 1.5;
    color: #ffe0e0;
    margin-bottom: 10px;
  }
  .confirm-text strong {
    color: #fff;
    font-family: var(--mono);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .confirm-actions {
    display: flex;
    gap: 9px;
  }
  .confirm-yes {
    flex: 1;
    min-height: 44px;
    padding: 10px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--danger-bright);
    background: var(--danger);
    color: #1a0606;
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    transition: filter 0.14s;
  }
  .confirm-yes:hover:not(:disabled) {
    filter: brightness(1.12);
  }
  .confirm-no {
    flex: 1;
    min-height: 44px;
    padding: 10px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line-strong);
    background: var(--bg-2);
    color: var(--ink-dim);
    font-size: 13px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    transition: border-color 0.14s, color 0.14s;
  }
  .confirm-no:hover:not(:disabled) {
    border-color: var(--ink-faint);
    color: var(--ink);
  }
  .confirm-yes:disabled,
  .confirm-no:disabled {
    opacity: 0.5;
  }

  /* spinners */
  .spin {
    width: 13px;
    height: 13px;
    border-radius: 50%;
    border: 2px solid rgba(230, 237, 243, 0.25);
    border-top-color: var(--cyan);
    animation: spin 0.7s linear infinite;
    flex: none;
  }
  .spin--danger {
    border-color: rgba(255, 77, 77, 0.3);
    border-top-color: var(--danger-bright);
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* output panel */
  .out {
    margin: 14px 16px 16px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line-strong);
    background: var(--bg-term);
    overflow: hidden;
  }
  .out--ok {
    border-color: var(--green-dim);
  }
  .out--fail {
    border-color: var(--danger-deep);
  }
  .out-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 11px;
    border-bottom: 1px solid var(--line);
    background: rgba(255, 255, 255, 0.02);
  }
  .out-verb {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink);
    letter-spacing: 0.04em;
  }
  .out-verb::before {
    content: '$ pve ';
    color: var(--ink-faint);
  }
  .out-status {
    font-family: var(--mono);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 2px 7px;
    border-radius: 4px;
    border: 1px solid currentColor;
  }
  .out-status--ok {
    color: var(--green);
  }
  .out-status--fail {
    color: var(--danger-bright);
  }
  .out-pre {
    margin: 0;
    padding: 11px 12px;
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.55;
    color: #c7d6e2;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 320px;
    overflow-y: auto;
  }
  .out-stderr-label {
    padding: 6px 12px 0;
    font-family: var(--mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--danger-bright);
  }
  .out-pre--stderr {
    color: #f3b6b6;
  }
  .out-pre--empty {
    color: var(--ink-faint);
    font-style: italic;
  }

  .block-error {
    margin: 14px 16px;
    padding: 11px 13px;
    border: 1px solid var(--danger-deep);
    border-left: 3px solid var(--danger);
    background: rgba(255, 77, 77, 0.07);
    border-radius: var(--radius-sm);
    color: #ffd5d5;
    font-size: 12.5px;
    line-height: 1.5;
  }
  .retry {
    margin-left: 8px;
    background: transparent;
    border: 1px solid var(--danger-deep);
    color: var(--danger-bright);
    border-radius: 5px;
    padding: 3px 9px;
    font-size: 11px;
  }
  .retry:hover {
    background: rgba(255, 77, 77, 0.12);
  }

  /* ── foldable sections (native <details>) ───────────────────────────────
     Each group + the output dump fold away on small screens. Open by default
     so nothing important is hidden; tap the header to collapse. */
  details.group > summary,
  details.out > summary {
    list-style: none;
    cursor: pointer;
    user-select: none;
  }
  details.group > summary::-webkit-details-marker,
  details.out > summary::-webkit-details-marker {
    display: none;
  }
  /* disclosure caret on the left of each foldable header */
  details.group > summary::before,
  details.out > summary::before {
    content: "▾";
    display: inline-block;
    width: 11px;
    margin-right: 4px;
    color: var(--ink-faint);
    font-size: 9px;
    transition: transform 0.15s ease;
  }
  details.group:not([open]) > summary::before,
  details.out:not([open]) > summary::before {
    transform: rotate(-90deg);
  }
  /* roomier tap target for the fold header on touch */
  details.group > summary {
    padding: 3px 0;
  }
  /* keep the exit-status pinned to the right now that a caret leads the row */
  .out-head .out-status {
    margin-left: auto;
  }
  /* a long dump (e.g. forensics) scrolls inside a capped box, not the page */
  .out-pre {
    max-height: 46vh;
    overflow: auto;
  }
</style>
