"""A small client for applications that send traces to AgentOps and read their live prompts from it.

    from agentops_client import AgentOps
    ops = AgentOps("http://localhost:3200", "ak_...")          # the key needs the prompts:read scope for get_prompt
    tracer = ops.init_tracing("checkout-assistant", "3.2.0")    # an OpenTelemetry tracer exporting to AgentOps
    prompt = ops.get_prompt("order-lookup")
    with tracer.start_as_current_span("chat model-fast") as span:
        prompt.stamp(span)                                      # records which prompt version produced this call
        text = prompt.render(input="Where is order 4417?")

This file has no dependency on the rest of the repository; copy it into an application.
"""

import re
import time
from dataclasses import dataclass

import httpx

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass
class Prompt:
    name: str
    version: int
    text: str
    variables: list[str]
    model_settings: dict

    def render(self, **values) -> str:
        missing = [v for v in self.variables if v not in values]
        if missing:
            raise ValueError(f"The prompt needs a value for: {', '.join(missing)}")
        return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), self.text)

    def stamp(self, span) -> None:
        span.set_attribute("agentops.prompt.name", self.name)
        span.set_attribute("agentops.prompt.version", self.version)


class AgentOps:
    def __init__(self, base_url: str, key: str, *, cache_seconds: float = 30, http: httpx.Client | None = None):
        self.base_url, self.key, self.cache_seconds = base_url.rstrip("/"), key, cache_seconds
        self.http = http or httpx.Client(timeout=5)
        self._prompts: dict[str, tuple[float, str, Prompt]] = {}  # name -> (fetched at, etag, prompt)

    def get_prompt(self, name: str) -> Prompt:
        """The live version. Cached briefly; if AgentOps cannot be reached, the last version seen is used, so an
        outage of this service does not take the application down with it."""
        cached = self._prompts.get(name)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[2]
        headers = {"Authorization": f"Bearer {self.key}"} | ({"If-None-Match": cached[1]} if cached else {})
        try:
            response = self.http.get(f"{self.base_url}/v1/prompts/{name}", headers=headers)
        except httpx.HTTPError:
            if cached:
                return cached[2]
            raise
        if response.status_code == 304 and cached:
            self._prompts[name] = (time.monotonic(), cached[1], cached[2])
            return cached[2]
        if response.status_code >= 500 and cached:
            return cached[2]
        response.raise_for_status()
        prompt = Prompt(**response.json())
        self._prompts[name] = (time.monotonic(), response.headers.get("ETag", ""), prompt)
        return prompt

    def init_tracing(self, service_name: str, service_version: str = ""):
        """Configure OpenTelemetry to export traces to AgentOps and return a tracer."""
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name, "service.version": service_version})
        )
        exporter = OTLPSpanExporter(
            endpoint=f"{self.base_url}/v1/traces", headers={"Authorization": f"Bearer {self.key}"}
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        return trace.get_tracer(service_name)
