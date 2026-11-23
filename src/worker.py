"""Cloudflare Worker entry point: serves the FastAPI app with D1 and R2 bindings."""

from workers import asgi

from app.main import app

Default = asgi.entrypoint(app)
