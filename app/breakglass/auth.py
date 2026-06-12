"""Auth for the breakglass app.

The app sits behind the ingress ``auth = "required"`` resilience proxy
(Authentik SSO normally, HTTP basic-auth fallback when Authentik is down), so a
browser request that reaches us is already edge-authenticated and carries the
proxy-injected ``X-authentik-username`` header. We also accept a bearer token
for machine/CLI callers. Either is sufficient.

When neither a token is configured nor a trusted header is present, we fail
closed.
"""
import hmac

from fastapi import Header, HTTPException

from . import config


def require_auth(
    authorization: str | None = Header(default=None),
    x_authentik_username: str | None = Header(default=None),
) -> str:
    """FastAPI dependency. Returns the identity (username or 'bearer'); raises
    401 otherwise."""
    # Edge-authenticated human: the auth-proxy sets this and overwrites any
    # client-supplied value, so its presence is trustworthy.
    if x_authentik_username:
        return x_authentik_username

    # Machine caller with the shared bearer token.
    if config.API_TOKEN and authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
        if hmac.compare_digest(token, config.API_TOKEN):
            return "bearer"

    raise HTTPException(status_code=401, detail="unauthenticated")
