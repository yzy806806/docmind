"""OpenTelemetry setup for docmind."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

from docmind.config import settings


def configure_telemetry() -> TracerProvider | None:
    """Configure OpenTelemetry tracing. Returns the provider or None if disabled."""
    if not settings.otel.enabled:
        return None

    resource = Resource.create({SERVICE_NAME: settings.otel.service_name})
    provider = TracerProvider(
        resource=resource,
        sampler=TraceIdRatioBased(settings.otel.sample_rate),
    )

    exporter = OTLPSpanExporter(endpoint=settings.otel.exporter_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    return provider


tracer = trace.get_tracer("docmind")
