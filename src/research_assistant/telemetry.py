"""Low-cardinality OpenTelemetry facade for the research pipeline."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Mapping


ALLOWED_ATTRIBUTES = {
    "cache_status",
    "embedding_model",
    "error_type",
    "evaluator_version",
    "fallback",
    "model",
    "provider",
    "refresh",
    "source_count",
    "stage",
    "status",
}
ERROR_TYPES = {
    "timeout",
    "dns",
    "http_4xx",
    "http_5xx",
    "parse_empty",
    "model_error",
    "invalid_result",
    "unknown",
}


class _NoOpSpan:
    def set_attribute(self, key: str, value: object) -> None:
        return None


class _NoOpSpanContext:
    def __enter__(self) -> _NoOpSpan:
        return _NoOpSpan()

    def __exit__(self, *args: object) -> None:
        return None


class _NoOpTracer:
    def start_as_current_span(self, name: str, attributes=None) -> _NoOpSpanContext:
        return _NoOpSpanContext()


class _NoOpInstrument:
    def add(self, value: int | float, attributes=None) -> None:
        return None

    def record(self, value: int | float, attributes=None) -> None:
        return None


class _NoOpMeter:
    def create_counter(self, name: str) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_histogram(self, name: str) -> _NoOpInstrument:
        return _NoOpInstrument()


class ResearchTelemetry:
    def __init__(self, enabled: bool = True, tracer=None, meter=None) -> None:
        if enabled and (tracer is None or meter is None):
            try:
                from opentelemetry import metrics, trace

                tracer = tracer or trace.get_tracer("research_assistant")
                meter = meter or metrics.get_meter("research_assistant")
            except ImportError:
                pass
        self.tracer = tracer or _NoOpTracer()
        self.meter = meter or _NoOpMeter()
        self.runs = self.meter.create_counter("research_runs_total")
        self.stage_duration = self.meter.create_histogram(
            "research_stage_duration_seconds"
        )
        self.search_results = self.meter.create_histogram("research_search_results")
        self.reads = self.meter.create_counter("research_read_total")
        self.cache = self.meter.create_counter("research_cache_total")
        self.chunks_indexed = self.meter.create_counter("research_chunks_indexed")
        self.retrieval_score = self.meter.create_histogram("research_retrieval_score")
        self.generation = self.meter.create_counter("research_generation_total")
        self.claim_coverage = self.meter.create_histogram("research_claim_coverage")
        self.claim_support = self.meter.create_histogram("research_claim_support_rate")
        self.citation_failures = self.meter.create_counter(
            "research_citation_failures_total"
        )
        self._providers: list[object] = []
        self._prometheus_server = None

    @asynccontextmanager
    async def stage(
        self, name: str, attributes: Mapping[str, object] | None = None
    ) -> AsyncIterator[object]:
        safe = sanitize_attributes({"stage": name, **(attributes or {})})
        started = time.perf_counter()
        status = "ok"
        with self.tracer.start_as_current_span(name, attributes=safe) as span:
            try:
                yield span
            except BaseException:
                status = "error"
                raise
            finally:
                duration_attributes = sanitize_attributes({**safe, "status": status})
                self.stage_duration.record(
                    time.perf_counter() - started, duration_attributes
                )

    def record_quality(self, quality) -> None:
        attributes = {"evaluator_version": quality.evaluator_version}
        self.claim_coverage.record(quality.claim_coverage, attributes)
        self.claim_support.record(quality.claim_support_rate, attributes)

    def shutdown(self) -> None:
        for provider in self._providers:
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()
        if self._prometheus_server:
            self._prometheus_server.shutdown()


def create_research_telemetry(config) -> ResearchTelemetry:
    """Create optional local SDK/exporters while keeping API-only setups valid."""
    if not config.enabled:
        return ResearchTelemetry(enabled=False)
    if not config.otlp_endpoint and not config.prometheus_port:
        return ResearchTelemetry(enabled=True)
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise RuntimeError(
            "configured research telemetry exporters require the 'observability' "
            "optional dependency group"
        ) from exc

    resource = Resource.create({"service.name": config.service_name})
    trace_provider = TracerProvider(resource=resource)
    metric_readers = []
    if config.otlp_endpoint:
        trace_exporter = OTLPSpanExporter(
            endpoint=_signal_endpoint(config.otlp_endpoint, "traces")
        )
        trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=_signal_endpoint(config.otlp_endpoint, "metrics")
                )
            )
        )
    prometheus_server = None
    if config.prometheus_port:
        from prometheus_client import start_http_server

        metric_readers.append(PrometheusMetricReader())
        prometheus_server = start_http_server(
            port=config.prometheus_port, addr="127.0.0.1"
        )[0]
    meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
    telemetry = ResearchTelemetry(
        enabled=True,
        tracer=trace_provider.get_tracer("research_assistant"),
        meter=meter_provider.get_meter("research_assistant"),
    )
    telemetry._providers = [trace_provider, meter_provider]
    telemetry._prometheus_server = prometheus_server
    return telemetry


def sanitize_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in attributes.items():
        if key not in ALLOWED_ATTRIBUTES or value is None:
            continue
        if key == "error_type":
            value = value if value in ERROR_TYPES else "unknown"
        if isinstance(value, (str, bool, int, float)):
            safe[key] = value
    return safe


def classify_error(error: object) -> str:
    value = str(error).casefold()
    if "timeout" in value:
        return "timeout"
    if "dns" in value or "name or service" in value:
        return "dns"
    if "404" in value or "403" in value or "401" in value or "4xx" in value:
        return "http_4xx"
    if "500" in value or "502" in value or "503" in value or "5xx" in value:
        return "http_5xx"
    if "empty" in value or "parse" in value:
        return "parse_empty"
    if "model" in value or "llm" in value:
        return "model_error"
    if "invalid" in value:
        return "invalid_result"
    return "unknown"


def _signal_endpoint(endpoint: str, signal: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith(("/v1/traces", "/v1/metrics")):
        base = base.rsplit("/v1/", 1)[0]
    return f"{base}/v1/{signal}"
