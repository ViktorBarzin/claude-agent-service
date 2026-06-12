<script>
  // A compact inline marker for a tool the agent ran mid-turn.
  //   ⚙ Bash: df -h        (Bash → show the command)
  //   ⚙ Read               (other tools → just the name)
  let { name = 'tool', command = '' } = $props();
</script>

<span class="chip" title={command ? `${name}: ${command}` : name}>
  <span class="cog" aria-hidden="true">⚙</span>
  <span class="name">{name}</span>
  {#if command}
    <span class="sep" aria-hidden="true">:</span>
    <code class="cmd">{command}</code>
  {/if}
</span>

<style>
  .chip {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    max-width: 100%;
    margin: 3px 4px 3px 0;
    padding: 3px 9px;
    border-radius: 6px;
    background: var(--bg-3);
    border: 1px solid var(--line-strong);
    border-left: 2px solid var(--cyan-dim);
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.45;
    vertical-align: baseline;
  }
  .cog {
    color: var(--cyan);
    font-size: 11px;
    transform: translateY(1px);
  }
  .name {
    color: var(--ink);
    font-weight: 600;
  }
  .sep {
    color: var(--ink-faint);
  }
  .cmd {
    color: var(--amber);
    font-family: var(--mono);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 100%;
  }
</style>
