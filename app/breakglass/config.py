"""Environment-driven config for the breakglass app.

Targets are hardcoded IPs by default (the breakglass must not depend on cluster
DNS — it has to work when things are broken). Everything is overridable via env
for tests and future re-IPing.
"""
import os

# SSH targets. IPs, not names — no DNS dependency in an incident.
DEVVM_HOST = os.environ.get("BREAKGLASS_DEVVM_HOST", "10.0.10.10")
DEVVM_USER = os.environ.get("BREAKGLASS_DEVVM_USER", "breakglass")
PVE_HOST = os.environ.get("BREAKGLASS_PVE_HOST", "192.168.1.127")
PVE_USER = os.environ.get("BREAKGLASS_PVE_USER", "root")

# The Claude agent the breakglass UI drives. Narrow tool surface, no web tools.
BREAKGLASS_AGENT = os.environ.get("BREAKGLASS_AGENT", "breakglass")
DEFAULT_MODEL = os.environ.get("BREAKGLASS_MODEL", "sonnet")

# Where claude session state + per-session scratch live. emptyDir in prod.
SESSIONS_DIR = os.environ.get("BREAKGLASS_SESSIONS_DIR", "/workspace/sessions")

# A single human operator per incident — no need for the job-runner's fan-out.
MAX_CONCURRENT_TURNS = int(os.environ.get("BREAKGLASS_MAX_CONCURRENT_TURNS", "2"))
# A chat turn that runs longer than this is killed (the agent is wedged).
TURN_TIMEOUT_SECONDS = int(os.environ.get("BREAKGLASS_TURN_TIMEOUT_SECONDS", "1800"))
# A single PVE power verb must return fast; a wedged host shouldn't hang the UI.
PVE_VERB_TIMEOUT_SECONDS = int(os.environ.get("BREAKGLASS_PVE_VERB_TIMEOUT_SECONDS", "120"))

# Auth. The app sits behind the ingress `auth = "required"` resilience proxy
# (Authentik SSO, basic-auth fallback when Authentik is down). We additionally
# accept a bearer token for machine/CLI callers. Either gate is sufficient;
# the edge is the primary one for the browser UI.
API_TOKEN = os.environ.get("API_BEARER_TOKEN", "")
# Header the auth-proxy injects for an authenticated human (set by Authentik, or
# by the basic-auth fallback's `$remote_user`). Presence ⇒ edge-authenticated.
TRUSTED_USER_HEADER = "x-authentik-username"
