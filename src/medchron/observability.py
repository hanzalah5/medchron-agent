"""OpenTelemetry tracing. It does nothing unless an OTLP endpoint is configured.

Set ``OTEL_EXPORTER_OTLP_ENDPOINT`` (and optionally ``OTEL_SERVICE_NAME``) to
send spans to any OTLP backend: Grafana Tempo, Jaeger, Honeycomb, or Langfuse,
which accepts OTLP. Span attributes carry IDs, counts, models, tokens and
cost, **never prompt or record text**.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

_configured = False


def setup_tracing(service: str = "medchron") -> bool:
    """Install an OTLP exporter when configured. Returns True if tracing is on."""
    global _configured
    if _configured or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return _configured
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service)})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    tracer = trace.get_tracer("medchron")
    with tracer.start_as_current_span(name) as s:
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, str | int | float | bool) else str(v))
        yield s
