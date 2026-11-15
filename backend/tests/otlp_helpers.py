"""Builds OTLP trace exports for tests and the demo generator."""

import gzip as gz
import json
import secrets
from datetime import UTC, datetime, timedelta

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue


def trace_id() -> str:
    return secrets.token_hex(16)


def span_id() -> str:
    return secrets.token_hex(8)


def span(
    name="chat model-fast",
    *,
    trace=None,
    sid=None,
    parent=None,
    start=None,
    ms=120,
    error=None,
    attrs=None,
    resource=None,
) -> dict:
    start = start or datetime.now(UTC) - timedelta(minutes=1)
    return {
        "trace_id": trace or trace_id(), "span_id": sid or span_id(), "parent_span_id": parent, "name": name,
        "start": start, "end": start + timedelta(milliseconds=ms), "error": error, "attributes": attrs or {},
        "resource": resource or {"service.name": "test-app"},
    }  # fmt: skip


def _any(value) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    return AnyValue(string_value=str(value))


def _json_any(value) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _nanos(t: datetime) -> int:
    return int(t.timestamp() * 1e9)


def make_batch(spans: list[dict], *, encoding: str = "protobuf", gzip: bool = False) -> tuple[bytes, dict]:
    """Returns (body, headers) for POST /v1/traces. Spans sharing a resource dict are grouped together."""
    if encoding == "protobuf":
        request = ExportTraceServiceRequest()
        for s in spans:
            rs = request.resource_spans.add()
            rs.resource.attributes.extend(KeyValue(key=k, value=_any(v)) for k, v in s["resource"].items())
            out = rs.scope_spans.add().spans.add()
            out.trace_id, out.span_id = bytes.fromhex(s["trace_id"]), bytes.fromhex(s["span_id"])
            if s["parent_span_id"]:
                out.parent_span_id = bytes.fromhex(s["parent_span_id"])
            out.name, out.kind = s["name"], 1
            out.start_time_unix_nano, out.end_time_unix_nano = _nanos(s["start"]), _nanos(s["end"])
            out.attributes.extend(KeyValue(key=k, value=_any(v)) for k, v in s["attributes"].items())
            if s["error"]:
                out.status.code, out.status.message = 2, s["error"]
        body, content_type = request.SerializeToString(), "application/x-protobuf"
    else:
        body = json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [{"key": k, "value": _json_any(v)} for k, v in s["resource"].items()]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": s["trace_id"],
                                        "spanId": s["span_id"],
                                        "parentSpanId": s["parent_span_id"] or "",
                                        "name": s["name"],
                                        "kind": 1,
                                        "startTimeUnixNano": str(_nanos(s["start"])),
                                        "endTimeUnixNano": str(_nanos(s["end"])),
                                        "attributes": [
                                            {"key": k, "value": _json_any(v)} for k, v in s["attributes"].items()
                                        ],
                                        "status": {"code": 2, "message": s["error"]} if s["error"] else {},
                                    }  # fmt: skip
                                ]
                            }
                        ],
                    }
                    for s in spans
                ]
            }
        ).encode()
        content_type = "application/json"
    headers = {"Content-Type": content_type}
    if gzip:
        body, headers["Content-Encoding"] = gz.compress(body), "gzip"
    return body, headers
