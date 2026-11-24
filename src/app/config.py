"""Settings come from Worker vars (wrangler.jsonc) and secrets (wrangler secret / .dev.vars)."""

from typing import Any, Literal

from pydantic import BaseModel, model_validator


class Settings(BaseModel):
    env: Literal["development", "production"] = "development"

    jwt_secret: str = ""
    session_days: int = 7
    cookie_name: str = "kc_session"
    cookie_domain: str | None = None  # ".karshachamling.org" in production

    cors_origins: str = "http://localhost:5173"

    media_public_base_url: str = "http://localhost:8787/media"
    max_upload_mb: int = 15

    cf_pages_deploy_hook: str = ""

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    min_donation_inr: int = 10

    turnstile_secret: str = ""

    # Show the interactive API docs (/docs) in production too
    enable_docs: bool = False

    resend_api_key: str = ""
    email_from: str = ""
    notify_email: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must be set to a random string of 32+ characters")
        return self

    @classmethod
    def from_env(cls, env: Any) -> "Settings":
        """Read each field from the Worker env object (or any object/dict), by UPPER_CASE name."""
        values = {}
        for name in cls.model_fields:
            key = name.upper()
            value = env.get(key) if isinstance(env, dict) else getattr(env, key, None)
            if value not in (None, ""):
                values[name] = str(value)
        return cls(**values)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.env == "production"
