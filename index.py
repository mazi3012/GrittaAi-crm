"""Vercel entrypoint for the Gretta CRM FastAPI application.

Vercel discovers root-level ``index.py`` files automatically and sends the
original request path to the ASGI application. Keeping the deployment shim at
the repository root lets ``/``, ``/api/*``, and ``/static/*`` resolve exactly
as they do when the app is run locally.
"""

import os
import sys

# Make imports deterministic when Vercel loads this file as the function
# entrypoint rather than starting Uvicorn from the repository root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard import app  # noqa: E402,F401
