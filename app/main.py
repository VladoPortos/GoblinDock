"""GoblinDock application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .api import router as api_router
from .config import settings
from .db import init_db
from .netutil import client_ip, set_request_ip
from .security import csrf_ok
from .session import ClockSkewTolerantSessionMiddleware
from .scheduler import start_scheduler, stop_scheduler
from .seed import run_all_seeds
from .worker import start_worker, stop_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("goblindock")

# Mutating API requests must carry a valid CSRF token (synchronizer-token pattern:
# token lives in the session, echoed by JS as X-CSRF-Token). Auth bootstrap is exempt.
CSRF_EXEMPT = {"/api/auth/login", "/api/auth/setup"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    run_all_seeds()
    start_worker()
    start_scheduler()
    log.info("GoblinDock ready · web=%s · vmid=%s-%s · max %sc/%sMB · dev=%s",
             settings.web_dir, settings.vmid_min, settings.vmid_max,
             settings.max_cores, settings.max_ram_mb, settings.dev_mode)
    yield
    stop_scheduler()
    stop_worker(join_timeout=30)


app = FastAPI(title="GoblinDock", version="1.0.0", lifespan=lifespan)


async def csrf_and_security_headers(request: Request, call_next):
    # Stamp the real client IP for this request so record_audit / the login throttle
    # see the user's address (not the reverse proxy's) — see app/netutil.py.
    set_request_ip(client_ip(request))
    if (request.url.path.startswith("/api/")
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.url.path not in CSRF_EXEMPT):
        if not csrf_ok(request.headers.get("x-csrf-token"), request.session.get("csrf")):
            return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; font-src 'self'; script-src 'self'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'",
    )
    return response


# Middleware is LIFO: add CSRF first (inner), Session last (outer) so the session
# is populated before the CSRF check reads request.session.
app.add_middleware(BaseHTTPMiddleware, dispatch=csrf_and_security_headers)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", "X-CSRF-Token"],
    )
app.add_middleware(
    # Skew-tolerant variant: WSL2 / VM clocks can jump backward, which would
    # otherwise make a freshly-signed session cookie look future-dated and decode as
    # empty → spurious 401 bursts → drop-to-login. See app/session.py.
    ClockSkewTolerantSessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.cookie_secure,
    max_age=14 * 24 * 3600,
)

app.include_router(api_router)


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "goblindock"}


class _RevalidatingStaticFiles(StaticFiles):
    """Serve the SPA assets with `Cache-Control: no-cache` so the browser ALWAYS
    revalidates (cheap ETag 304 when unchanged) and never silently runs a stale
    bundle after an update. Without this, the unversioned <script src> tags get
    heuristically cached and UI changes don't appear until a manual hard-refresh."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Static SPA (the React prototype, wired to the API). Mounted last so /api wins.
app.mount("/", _RevalidatingStaticFiles(directory=str(settings.web_dir), html=True), name="web")
