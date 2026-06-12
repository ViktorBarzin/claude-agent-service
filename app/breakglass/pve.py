"""PVE power verbs — the LLM-independent recovery path.

The manual UI buttons hit this directly (no ``claude`` in the path), so reset
works even when the Anthropic API is down. The real enforcement is the
forced-command on the PVE host (``/usr/local/bin/breakglass-pve``): whatever we
send as the SSH command is ignored except as ``$SSH_ORIGINAL_COMMAND``, and the
host script only honours the verbs below against VM 102. We validate here too —
defense in depth + a clean error before a round-trip.

All subprocesses use ``asyncio.create_subprocess_exec`` (list argv, no shell),
so the verb string is never interpreted by a shell — there is no injection
surface even though the allowlist already constrains the input.
"""
import asyncio
from subprocess import PIPE

from . import config

# Must mirror /usr/local/bin/breakglass-pve on the PVE host.
ALLOWED_VERBS: frozenset[str] = frozenset(
    {"status", "forensics", "reset", "stop", "start", "cycle"}
)
# Verbs that change VM state — the UI flags these for an explicit confirm and
# the host script captures forensics before running them.
MUTATING_VERBS: frozenset[str] = frozenset({"reset", "stop", "start", "cycle"})

def _ssh_argv(user: str, host: str, remote_command: str) -> list[str]:
    """Build an ssh argv (list form, no shell). ``remote_command`` is passed as
    a single token; on the PVE host the forced-command ignores it except as
    ``$SSH_ORIGINAL_COMMAND``.

    Host-key checking is disabled deliberately: a devvm REBUILD changes its host
    key (e.g. 2026-05-23), and strict checking would lock the breakglass out at
    exactly the moment it's needed. The targets are on the trusted internal LAN;
    availability beats MITM hardening here. Auth is still by key (ssh-agent)."""
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        f"{user}@{host}",
        remote_command,
    ]


def is_allowed(verb: str) -> bool:
    return verb in ALLOWED_VERBS


async def run_verb(verb: str, timeout: float | None = None) -> dict:
    """Run a single PVE verb against VM 102 over the forced-command SSH key.

    Returns ``{"verb", "exit_code", "stdout", "stderr", "rejected"}``. A verb
    not in the allowlist is rejected locally (``rejected=True``) without any
    SSH at all.
    """
    if verb not in ALLOWED_VERBS:
        return {
            "verb": verb,
            "exit_code": None,
            "stdout": "",
            "stderr": f"rejected: '{verb}' is not an allowed verb",
            "rejected": True,
        }

    timeout = timeout if timeout is not None else config.PVE_VERB_TIMEOUT_SECONDS
    argv = _ssh_argv(config.PVE_USER, config.PVE_HOST, verb)
    proc = await asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "verb": verb,
            "exit_code": None,
            "stdout": "",
            "stderr": f"timeout after {timeout}s talking to PVE host",
            "rejected": False,
        }
    return {
        "verb": verb,
        "exit_code": proc.returncode,
        "stdout": out.decode(errors="replace"),
        "stderr": err.decode(errors="replace"),
        "rejected": False,
    }
