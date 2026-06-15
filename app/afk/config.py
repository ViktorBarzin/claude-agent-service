"""Config loader for the AFK loop — DISABLED BY DEFAULT.

The whole loop ships off. A bare ``Config()`` (and therefore ``default()``,
``from_env()`` with nothing set, and ``from_configmap({})``) has
``kill_switch=True`` and an empty ``allowlist`` — so nothing is ever
dispatched until an operator deliberately turns it on. Enabling is a TWO-part
manual step, on purpose:

  1. set ``AFK_KILL_SWITCH=false`` (or ``kill_switch: "false"`` in the
     ConfigMap), AND
  2. populate ``AFK_ALLOWLIST`` with the exact repos that may be automated.

Either alone is inert: the kill switch off with an empty allowlist still
dispatches nothing, and a full allowlist with the kill switch on is frozen.
Both gates exist so a single fat-fingered env var can't accidentally arm the
loop across every repo.

``from_env`` reads process env; ``from_configmap`` reads an already-parsed
string→string mapping (the shape a mounted ConfigMap gives you). They share one
parser so the two paths can't drift. Lists are comma-separated; booleans accept
the usual truthy spellings.

This module owns only *loading* a ``Config`` — the dataclass itself lives in
``types`` and policy decisions live in ``dispatch_policy`` / ``run_state_machine``.
"""
import os
from collections.abc import Mapping

from .types import Config

# Env var names — also the ConfigMap keys (one source of truth for both paths).
ENV_ALLOWLIST = "AFK_ALLOWLIST"
ENV_KILL_SWITCH = "AFK_KILL_SWITCH"
ENV_IN_PROGRESS_LABEL = "AFK_IN_PROGRESS_LABEL"
ENV_READY_LABEL = "AFK_READY_LABEL"
ENV_BUDGET_USD = "AFK_BUDGET_USD"
ENV_FIX_FORWARD_MAX_ATTEMPTS = "AFK_FIX_FORWARD_MAX_ATTEMPTS"
ENV_FIX_FORWARD_MAX_SECONDS = "AFK_FIX_FORWARD_MAX_SECONDS"

# Spellings accepted as boolean true / false (case-insensitive). Anything else
# raises rather than silently defaulting — an unparseable kill-switch value must
# never be guessed safe-or-unsafe.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def default() -> Config:
    """The disabled default Config: kill switch ON, allowlist EMPTY.

    Equivalent to ``Config(allowlist=[], kill_switch=True)``; provided as a named
    entry point so callers don't hardcode the disabled posture themselves.
    """
    return Config(allowlist=[], kill_switch=True)


def from_env(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from environment variables (defaults to ``os.environ``).

    Unset variables fall back to the disabled/contract defaults, so an
    unconfigured process stays off.
    """
    return _from_mapping(os.environ if env is None else env)


def from_configmap(data: Mapping[str, str]) -> Config:
    """Build a Config from a parsed ConfigMap (string→string mapping).

    Identical semantics to ``from_env`` — same keys, same parser — but sourced
    from a mounted ConfigMap's ``data`` rather than process env. An empty mapping
    yields the disabled default.
    """
    return _from_mapping(data)


# --------------------------------------------------------------------------- #
# Internals — one shared parser so env and ConfigMap paths can't diverge.
# --------------------------------------------------------------------------- #
def _from_mapping(data: Mapping[str, str]) -> Config:
    base = default()
    return Config(
        allowlist=_parse_list(data.get(ENV_ALLOWLIST), base.allowlist),
        kill_switch=_parse_bool(data.get(ENV_KILL_SWITCH), base.kill_switch),
        in_progress_label=_nonempty(data.get(ENV_IN_PROGRESS_LABEL), base.in_progress_label),
        ready_label=_nonempty(data.get(ENV_READY_LABEL), base.ready_label),
        budget_usd=_parse_float(data.get(ENV_BUDGET_USD), base.budget_usd),
        fix_forward_max_attempts=_parse_int(
            data.get(ENV_FIX_FORWARD_MAX_ATTEMPTS), base.fix_forward_max_attempts
        ),
        fix_forward_max_seconds=_parse_int(
            data.get(ENV_FIX_FORWARD_MAX_SECONDS), base.fix_forward_max_seconds
        ),
    )


def _parse_list(raw: str | None, fallback: list[str]) -> list[str]:
    if raw is None:
        return list(fallback)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_bool(raw: str | None, fallback: bool) -> bool:
    if raw is None:
        return fallback
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"unparseable boolean for AFK config: {raw!r}")


def _parse_int(raw: str | None, fallback: int) -> int:
    if raw is None or not raw.strip():
        return fallback
    return int(raw.strip())


def _parse_float(raw: str | None, fallback: float) -> float:
    if raw is None or not raw.strip():
        return fallback
    return float(raw.strip())


def _nonempty(raw: str | None, fallback: str) -> str:
    if raw is None or not raw.strip():
        return fallback
    return raw.strip()
