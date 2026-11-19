import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.api.routes import router
from app.sre import routes as sre_routes
from app.sre.a2a_specialists import build_a2a_app, llm_enabled
from app.sre.monitor import monitor_loop
from app.sre.otel import configure_tracing
from app.sre.pipeline import SREOrchestrator
from app.sre.store import StateStore

logger = logging.getLogger("aegisops")
for _noisy in ("mcp", "httpx", "a2a"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def create_app() -> FastAPI:
    public_url = os.environ.get("AEGIS_PUBLIC_URL", "http://localhost:8000").rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.warning("AegisOps reasoning mode: %s", "LLM (CrewAI)" if llm_enabled()
                       else "deterministic - set GEMINI_API_KEY or OPENAI_API_KEY for LLM reasoning")
        resumed = await sre_routes.ctx.orch.resume_pending()
        if resumed:
            logger.warning("Resumed %d interrupted investigation(s).", resumed)
        monitor = asyncio.create_task(monitor_loop(sre_routes.ctx.store, sre_routes.ctx.orch, sre_routes.ctx.tasks))
        yield
        monitor.cancel()

    app = FastAPI(
        title="AegisOps",
        version="1.0.0",
        description="Agentic AI incident investigation and controlled self-healing platform "
                    "(Google ADK + CrewAI + MCP + A2A).",
        lifespan=lifespan,
    )
    configure_tracing()
    store = StateStore()
    # A2A always speaks JSON-RPC over HTTP. "inprocess" sends those HTTP requests to
    # this same app through ASGI (single container); "network" uses AEGIS_PUBLIC_URL.
    transport = httpx.ASGITransport(app=app) if os.environ.get("AEGIS_A2A_MODE", "inprocess") == "inprocess" else None
    sre_routes.ctx.store = store
    sre_routes.ctx.orch = SREOrchestrator(store, a2a_base_url=public_url, a2a_transport=transport)
    app.mount("/a2a", build_a2a_app(public_url))
    app.include_router(sre_routes.router, prefix="/api")
    app.include_router(router, prefix="/api")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/api/v1/console")

    return app


app = create_app()
