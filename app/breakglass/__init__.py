"""Breakglass: an isolated emergency-recovery surface for the devvm.

This package is a SEPARATE ASGI app from ``app.main``. The breakglass
deployment runs ``uvicorn app.breakglass.server:app`` and mounts the SSH keys;
the ordinary claude-agent-service deployment keeps running ``app.main:app`` and
never sees those keys. Nothing here imports ``app.main`` and vice versa, so the
untrusted-input agents (recruiter-triage, nextcloud-todos) can never share a
process with the root-on-devvm / PVE-reset credentials. See
``docs/adr/0001-breakglass-security-architecture.md``.
"""
