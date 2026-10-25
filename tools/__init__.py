"""Real-data test tools for the owner (see tools/README.md).

They talk to the owner's Home Assistant with a long-lived token, never to the cars' or
Solcast's clouds directly, and only ``attended_test.py`` may call a service. Each tool runs
as a script (``python tools/check.py``) or a module (``python -m tools.check``).
"""
