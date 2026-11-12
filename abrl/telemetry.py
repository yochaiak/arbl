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
