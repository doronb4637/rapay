"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from gsim.api.routes import behaviours, connections, events, messages
from gsim.core_gateway import get_runtime

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


class _WebFiles(StaticFiles):
    """The built UI, with `index.html` explicitly never cached.

    Vite fingerprints every asset (`index-<hash>.js`), so those are safe to
    cache forever -- a rebuild produces a NEW filename. `index.html` is the one
    file with a stable name, and it is the only thing that names the current
    hashes. If the shell caches it, the app keeps loading the *previous*
    build's bundle indefinitely: rebuilding changes nothing on screen, and a
    fix that is provably present in `dist/` appears not to work. That failure
    mode is invisible from the outside -- the UI simply behaves like an older
    version -- so it is worth one header to make impossible.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path in ("", ".", "index.html") or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_runtime()          # build the runtime (and core's event-loop thread) up front
    yield
    get_runtime().shutdown()   # absolute teardown of every managed connection


def create_app() -> FastAPI:
    app = FastAPI(
        title="GSim API",
        version="0.1.0",
        summary="Generic Simulator -- UI and API over the core connection framework.",
        lifespan=lifespan,
    )

    # Vite's dev server runs on a different port; in the packaged desktop app
    # the UI is same-origin so this is a no-op.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(connections.router)
    app.include_router(messages.router)
    app.include_router(messages.registry_router)
    app.include_router(behaviours.router)
    app.include_router(events.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Serve the built React bundle when it exists, so the desktop shell has a
    # single origin to point at. Mounted last: it claims "/".
    if WEB_DIST.is_dir():
        app.mount("/", _WebFiles(directory=WEB_DIST, html=True), name="web")

    return app


app = create_app()
