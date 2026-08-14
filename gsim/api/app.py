"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from gsim.api.routes import connections, events, messages
from gsim.core_gateway import get_runtime

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


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
    app.include_router(events.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Serve the built React bundle when it exists, so the desktop shell has a
    # single origin to point at. Mounted last: it claims "/".
    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")

    return app


app = create_app()
