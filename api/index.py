"""
api/index.py
-------------
Vercel's Python runtime looks for a Flask `app` object at this exact
path (api/index.py) by default -- this is the one new file that makes
your EXISTING webhook_server.py deployable, without renaming or
restructuring anything you already have.

This file does nothing except import the real app. All your actual
routes (/webhook, /run-checks, /telegram-webhook, /) are defined in
webhook_server.py exactly as before -- this is just the entry point
Vercel's build process looks for.
"""

from webhook_server import app
