"""ConanOps' web version: the app's features in a browser, locally or via a
Cloudflare tunnel.

auth.py (sessions, lockout), bridge.py (runs actions on the GUI thread so the
web shares the app's code paths), api.py (JSON API), fields.py (generic
settings-page access), server.py (HTTP server), tunnel.py (Cloudflare tunnel).
"""
