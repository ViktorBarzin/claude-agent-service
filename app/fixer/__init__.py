"""The fixer: a `broken` Forgejo issue repairs itself.

See docs/2026-08-25-forgejo-fixer-design.md. This package holds the pieces that
are specific to the fixer capability; the loop mechanics it drives (dispatch
policy, run state machine, CI watcher, notifier) live in ``app.afk``.
"""
