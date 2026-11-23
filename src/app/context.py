"""Per-request access to settings, D1 and R2.

In the Worker, bindings live on request.scope["env"]. Tests set app.state.test_ctx instead.
"""

from dataclasses import dataclass

from fastapi import Request

from app.config import Settings
from app.db import D1Database, Database
from app.storage import R2Storage, Storage


@dataclass
class Ctx:
    settings: Settings
    db: Database
    storage: Storage


_settings_cache: Settings | None = None


def ctx_for_scope(app, scope) -> Ctx:
    env = scope.get("env")
    if env is None:
        return app.state.test_ctx
    global _settings_cache
    if _settings_cache is None:  # vars/secrets are fixed for the life of the isolate
        _settings_cache = Settings.from_env(env)
    return Ctx(_settings_cache, D1Database(env.DB), R2Storage(env.MEDIA))


def get_ctx(request: Request) -> Ctx:
    return ctx_for_scope(request.app, request.scope)
