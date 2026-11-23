from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.context import get_ctx
from app.routers import admin_content, admin_inbox, admin_media, auth, donations, public

ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE"
ALLOWED_HEADERS = "Content-Type, X-Requested-With"
DOCS_PATHS = ("/docs", "/openapi.json")


def create_app() -> FastAPI:
    app = FastAPI(title="Karsha Chamling API", redoc_url=None)

    @app.middleware("http")
    async def cors_and_docs(request: Request, call_next):
        """CORS + hiding API docs in production. Done by hand because the allowed origins
        come from Worker vars, which are only known per request."""
        settings = get_ctx(request).settings
        if settings.is_production and request.url.path.startswith(DOCS_PATHS):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        origin = request.headers.get("origin")
        allowed = bool(origin) and origin in settings.cors_origin_list
        preflight = request.method == "OPTIONS" and "access-control-request-method" in (
            request.headers
        )
        if preflight:
            response = Response(status_code=204 if allowed else 400)
        else:
            response = await call_next(request)
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
            if preflight:
                response.headers["Access-Control-Allow-Methods"] = ALLOWED_METHODS
                response.headers["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
                response.headers["Access-Control-Max-Age"] = "86400"
        return response

    app.include_router(public.router)
    app.include_router(donations.router)
    app.include_router(auth.router)
    for r in admin_content.routers:
        app.include_router(r)
    app.include_router(admin_media.router)
    app.include_router(admin_inbox.router)

    @app.get("/health", tags=["meta"])
    async def health():
        return {"ok": True}

    return app


app = create_app()
