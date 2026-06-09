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
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from dexter.mbta._timeutils import MBTA_TZ

logger = logging.getLogger("dexter.service")

# When this process started — a good proxy for "last deployed" since the warm
# replica restarts on each new revision. Used unless DEXTER_DEPLOYED_AT overrides it.
_STARTED_AT = datetime.now(UTC)


def _format_et(raw: str | datetime | None) -> str:
    """Render a UTC timestamp as a readable US/Eastern string for the UI footer.

    Accepts an ISO-8601 string (e.g. DEXTER_DEPLOYED_AT) or a datetime; returns ""
    when missing/unparseable so the client shows 'unavailable'.
    """
    if raw is None:
        return ""
    when = raw
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(MBTA_TZ).strftime("%b %d, %Y · %I:%M %p ET")


# Static terminal UI lives in web/ (sibling to dexter/), kept fully separate from
# the app layers and served only when dexter_serve_web is on. Resolve it against
# the working directory first (the container copies web/ next to the run dir, and
# local runs start from the repo root) and fall back to the source-relative path
# (covers editable installs and tests). First existing file wins.
def _find_web_index() -> Path:
    candidates = (
        Path.cwd() / "web" / "index.html",
        Path(__file__).resolve().parents[2] / "web" / "index.html",
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]  # nonexistent; the route returns 404


_WEB_INDEX = _find_web_index()


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
    from dexter.observability import configure_tracing
    from dexter.profiles import CommuteStore

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    configure_tracing(settings)  # opt-in; no-op unless DEXTER_TRACING is set

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

    store = CommuteStore(settings.dexter_db_path)
    await store.init()  # create the saved_commute table if needed
    logger.info("commute store ready at %s", settings.dexter_db_path)

    app.state.graph = build_graph(
        router=router, resolver=resolver, departures=departures, store=store
    )
    app.state.mbta_client = mbta
    app.state.azure_client = azure
    app.state.owns_clients = True
    # Beta web client + gate, sourced from config here (lifespan) so importing this
    # module never forces Settings validation. Tests inject a graph and skip this,
    # leaving the defaults set in create_app (gate off, web off).
    app.state.passcode = settings.dexter_passcode
    # Prefer an explicit deploy time from the deploy script; fall back to when this
    # process started. Formatted to Eastern here so the static client stays dumb.
    app.state.deployed_at = _format_et(settings.dexter_deployed_at) or _format_et(_STARTED_AT)
    app.state.serve_web = settings.dexter_serve_web


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
    # Safe defaults; _build_runtime overrides these from config when the service
    # owns its dependencies. Tests that inject a graph keep the gate/web off.
    app.state.passcode = None
    app.state.deployed_at = _format_et(_STARTED_AT)
    app.state.serve_web = False

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "deployed_at": app.state.deployed_at}

    @app.get("/")
    async def index() -> FileResponse:
        # Dumb static client (the CLI's twin); only present when explicitly enabled.
        if not app.state.serve_web or not _WEB_INDEX.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(_WEB_INDEX)

    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        request: ChatRequest,
        x_dexter_passcode: str | None = Header(default=None),
        x_dexter_user: str | None = Header(default=None),
    ) -> ChatResponse:
        from dexter.observability import session

        # Gate before any graph/LLM work so a leaked link can't spend Azure quota.
        if app.state.passcode and x_dexter_passcode != app.state.passcode:
            raise HTTPException(status_code=401, detail="Invalid or missing passcode.")

        # Opaque per-rider token (saved commutes); separate from the passcode gate.
        config = {"configurable": {"thread_id": request.session_id}}
        try:
            with session(request.session_id):  # groups this turn's spans by conversation
                state = await app.state.graph.ainvoke(
                    {"message": request.message, "user_id": x_dexter_user}, config
                )
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
