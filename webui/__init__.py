"""ConanOps' web version: the app's features in a browser, on the home
network or (through a Cloudflare tunnel) from anywhere.

  auth.py      passwords, sessions, login lockout
  bridge.py    runs actions on the app's own (GUI) thread, so the web
               uses exactly the same code paths as clicking in the app
  api.py       the JSON API -- one function per action
  fields.py    reads/writes the app's settings pages generically
  server.py    the HTTP server, static files and security headers
  tunnel.py    the optional Cloudflare tunnel for access from anywhere
"""
