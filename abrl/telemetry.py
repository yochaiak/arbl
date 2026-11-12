import json
import logging
import os
from datetime import UTC, datetime


class JsonLog(logging.Formatter):
    def format(self, record):
        fields = {
            "ts": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if trial := getattr(record, "trial_id", None):
            fields["trial_id"] = trial
        if record.exc_info:
            fields["exception_type"] = record.exc_info[0].__name__
        return json.dumps(fields)


def configure_logging() -> None:
    logger = logging.getLogger("abrl")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLog())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def instrument(app) -> None:
    # Export only when the operator explicitly configures an OTLP collector.
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") and not os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "abrl-api"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    # Use a fixed header allowlist (none by default). Do not collect lab requests.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="/lab,/health,/metrics",
        http_capture_headers_server_request=[],
        http_capture_headers_server_response=[],
    )


def trace_session(function):
    """Trace each durable assignment without recording URLs, input values or secrets."""
    from functools import wraps

    from opentelemetry import trace

    configured = False

    @wraps(function)
    async def wrapped(work):
        nonlocal configured
        if not configured:
            configured = True
            if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                provider = TracerProvider(resource=Resource.create({"service.name": "abrl-session-runner"}))
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
                trace.set_tracer_provider(provider)
        with trace.get_tracer("abrl.session").start_as_current_span("session.execute") as span:
            span.set_attributes(
                {
                    "abrl.session_id": work.id,
                    "abrl.attempt_id": work.lease,
                    "abrl.adapter": work.adapter,
                    "abrl.mode": work.mode,
                    "abrl.ownership_epoch": work.epoch,
                }
            )
            return await function(work)

    return wrapped
