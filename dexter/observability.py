"""Optional OpenTelemetry tracing via Arize Phoenix (OpenInference).

Off by default. When ``DEXTER_TRACING`` is set, :func:`configure_tracing` registers a
Phoenix OTLP exporter with a background ``BatchSpanProcessor`` and auto-instruments
LangGraph + the OpenAI client. Until then the global OTel tracer is a no-op, so
instrumentation adds ~no latency and the heavy tracing dependencies are never
imported. Secrets are never recorded on spans.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

logger = logging.getLogger("dexter.observability")

_configured = False


def configure_tracing(settings) -> bool:
    """Enable Phoenix/OTel tracing when ``DEXTER_TRACING`` is set.

    Idempotent and a no-op when disabled. Returns True if tracing was activated.
    Heavy imports happen only here, so the rest of the app stays light when off.
    """
    global _configured
    if _configured or not getattr(settings, "dexter_tracing", False):
        return False

    try:
        from phoenix.otel import register
    except ImportError:
        logger.warning(
            "DEXTER_TRACING is on but tracing deps are missing — "
            "install them with `uv sync --extra tracing`. Continuing without tracing."
        )
        return False

    kwargs = {"project_name": "dexter", "auto_instrument": True, "batch": True}
    endpoint = getattr(settings, "dexter_tracing_endpoint", None)
    if endpoint:
        kwargs["endpoint"] = endpoint
    # Phoenix Cloud authenticates the OTLP exporter via an `api_key` header.
    # Absent (local Phoenix) this stays unset, preserving prior behavior.
    api_key = getattr(settings, "dexter_tracing_api_key", None)
    if api_key:
        kwargs["headers"] = {"api_key": api_key}
    register(**kwargs)

    _configured = True
    logger.info("tracing enabled (Phoenix / OpenInference)")
    return True


@contextlib.contextmanager
def session(session_id: str) -> Iterator[None]:
    """Group spans created in this block under a conversation/session id.

    A no-op when the OpenInference package isn't installed (tracing off).
    """
    try:
        from openinference.instrumentation import using_session
    except ImportError:
        yield
        return
    with using_session(session_id):
        yield
