"""GoblinDock application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .api import router as api_router
from .template_ops import router as template_router
from .operations import router as operations_router
from .inventory import start_inventory, stop_inventory
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
    # If no admin exists yet (and we're not in dev / env-seeded), print the first-run
    # setup token so the operator — and only the operator, who can read these logs — can
    # complete the unauthenticated /auth/setup without an internet attacker racing them.
    from .api import announce_setup_token
    from .db import session_scope
    with session_scope() as _s:
        announce_setup_token(_s)
    start_worker()
    start_scheduler()
    start_inventory()
    log.info("GoblinDock ready · web=%s · vmid=%s-%s · max %sc/%sMB · dev=%s",
             settings.web_dir, settings.vmid_min, settings.vmid_max,
             settings.max_cores, settings.max_ram_mb, settings.dev_mode)
    yield
    stop_scheduler()
    stop_inventory()
    stop_worker(join_timeout=30)


app = FastAPI(title="GoblinDock", version=__version__, lifespan=lifespan)
app.include_router(template_router)
app.include_router(operations_router)


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before FastAPI parses JSON or multipart data.

    Content-Length is a fast path; buffering at most the configured cap also covers
    chunked requests whose size is not declared up front.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    async def _reject(self, scope, receive, send):
        response = JSONResponse(
            {"detail": "request body too large"},
            status_code=413,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers") or []:
            if name.lower() != b"content-length":
                continue
            try:
                if int(value) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except (TypeError, ValueError):
                pass

        messages = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") == "http.request":
                total += len(message.get("body") or b"")
                if total > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break

        async def replay_receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


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
    # Tight CSP: scripts/styles/connect are 'self' only (no unsafe-eval, no remote
    # origins). The one exception is style-src 'unsafe-inline' — the React SPA renders
    # via inline `style=` attributes (h(..., {style:{...}})), so dropping it would break
    # all styling. Removing it would require nonce/hash-ing every inline style (a build
    # refactor); accepted as-is. script-src deliberately has NO 'unsafe-inline'.
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
# Added last so it is the outermost HTTP middleware and rejects oversized bodies
# before session decoding, CSRF work, request parsing, or password hashing.
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_request_body_bytes)

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
