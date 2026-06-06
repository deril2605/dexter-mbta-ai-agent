"""FastAPI brain — the HTTP service in front of the agent graph (PRD §7.1).

Exposes ``POST /chat`` and ``GET /health``. On startup it wires the real
dependencies (Azure router, MBTA client, resolver, departures) and warms the
route cache, then builds the LangGraph agent once. Conversation state is kept by
the graph's checkpointer, keyed by ``session_id``.

Tests inject a pre-built graph via ``create_app(graph=...)`` so no live calls or
credentials are needed.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger("dexter.service")


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    needs_input: bool


async def _build_runtime(app: FastAPI) -> None:
    """Construct real dependencies from config and warm the route cache."""
    # Imported lazily so importing this module (and tests) never forces config
    # validation or network access.
    from openai import AsyncAzureOpenAI

    from dexter.agent.graph import build_graph
    from dexter.agent.router import Router
    from dexter.config import get_settings
    from dexter.mbta.client import MBTAClient
    from dexter.mbta.predictions import DeparturesService
    from dexter.mbta.resolution import Resolver
    from dexter.mbta.routes import RouteCache

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    azure = AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )
    mbta = MBTAClient(base_url=settings.mbta_base_url, api_key=settings.mbta_api_key)

    route_cache = RouteCache(mbta)
    await route_cache.refresh()  # warm the route cache on startup (PRD §7.1)
    logger.info("route cache warmed")

    router = Router(azure, settings.azure_openai_deployment_router)
    resolver = Resolver(mbta, route_cache)
    departures = DeparturesService(mbta)

    app.state.graph = build_graph(router=router, resolver=resolver, departures=departures)
    app.state.mbta_client = mbta
    app.state.azure_client = azure
    app.state.owns_clients = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(app.state, "graph", None) is None:
        await _build_runtime(app)
    try:
        yield
    finally:
        if getattr(app.state, "owns_clients", False):
            await app.state.mbta_client.aclose()
            await app.state.azure_client.close()


def create_app(*, graph=None) -> FastAPI:
    app = FastAPI(title="Dexter", version="0.1.0", lifespan=lifespan)
    app.state.graph = graph
    app.state.owns_clients = False

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        config = {"configurable": {"thread_id": request.session_id}}
        try:
            state = await app.state.graph.ainvoke({"message": request.message}, config)
        except Exception:  # noqa: BLE001 - never leak a stack trace to the client
            logger.exception("chat turn failed for session %s", request.session_id)
            return ChatResponse(
                reply="Sorry — I hit a problem handling that. Please try again.",
                needs_input=False,
            )
        return ChatResponse(
            reply=state.get("reply", ""),
            needs_input=bool(state.get("needs_input", False)),
        )

    return app


# Module-level app for `uvicorn dexter.service.app:app`.
app = create_app()
