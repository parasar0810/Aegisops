"""OpenTelemetry setup. Exports OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set
(Jaeger, Tempo, or Langfuse's OTLP endpoint); otherwise spans stay local and the
incident timeline + audit log + Prometheus metrics are the local fallback."""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)
_configured = False


def configure_tracing() -> str:
    global _configured
    if _configured:
        return "already-configured"
    _configured = True
    provider = TracerProvider(resource=Resource.create({"service.name": "aegisops"}))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    mode = "local"
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        mode = f"otlp:{endpoint}"
    try:
        trace.set_tracer_provider(provider)
    except Exception:  # another library already set one; keep it
        logger.info("Tracer provider already set; reusing it.")
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        logger.warning("Langfuse credentials not set; LLM traces are local only (see SECURITY/DEPLOYMENT docs).")
    return mode
