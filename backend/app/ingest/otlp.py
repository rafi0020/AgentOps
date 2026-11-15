"""Reads OpenTelemetry trace exports (OTLP over HTTP) in both encodings the protocol defines.

Protobuf is parsed with the official message classes. JSON is walked by hand: OTLP's JSON form writes trace
and span ids as hex, which the generic protobuf JSON parser would misread as base64.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

PROTOBUF, JSON = "application/x-protobuf", "application/json"
STATUS = {
    0: "unset",
    1: "ok",
    2: "error",
    "STATUS_CODE_UNSET": "unset",
    "STATUS_CODE_OK": "ok",
    "STATUS_CODE_ERROR": "error",
}
MAX_SPANS_PER_BATCH = 10_000


class InvalidPayload(ValueError):
    """The body is not a valid OTLP trace export. The whole batch is refused."""


class SpanRejected(ValueError):
    """One span cannot be stored. Only that span is dropped; the rest of the export is kept."""


@dataclass
class ParsedBatch:
    spans: list = field(default_factory=list)
    rejected: int = 0
    reasons: list = field(default_factory=list)  # a few distinct reasons, reported in the OTLP response

    def reject(self, reason: str) -> None:
        self.rejected += 1
        if reason not in self.reasons and len(self.reasons) < 5:
            self.reasons.append(reason)


@dataclass
class RawSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: int
    start: datetime
    end: datetime
    status: str
    status_message: str
    attributes: dict
    resource: dict
    events: list = field(default_factory=list)


# Everything a malformed span can raise while it is being read: bad numbers, dates out of range, missing keys.
SPAN_ERRORS = (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, OSError)


def _time(nanos: int | str) -> datetime:
    try:
        return datetime.fromtimestamp(int(nanos) / 1e9, UTC)
    except SPAN_ERRORS as exc:
        raise SpanRejected("A span has a timestamp outside the range of dates.") from exc


def _pb_value(v):
    kind = v.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [_pb_value(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: _pb_value(kv.value) for kv in v.kvlist_value.values}
    if kind == "bytes_value":
        return v.bytes_value.hex()
    return getattr(v, kind)


def _json_value(v: dict):
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [_json_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: _json_value(kv.get("value", {})) for kv in v["kvlistValue"].get("values", [])}
    return None


def _checked(s: RawSpan) -> RawSpan:
    if (
        len(s.trace_id) != 32
        or len(s.span_id) != 16
        or not all(c in "0123456789abcdef" for c in s.trace_id + s.span_id)
    ):
        raise SpanRejected("A span has a malformed trace or span id.")
    if s.end < s.start:
        raise SpanRejected("A span ends before it starts.")
    return s


def _from_protobuf(body: bytes) -> ParsedBatch:
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except DecodeError as exc:
        raise InvalidPayload("The body is not a valid protobuf trace export.") from exc
    batch = ParsedBatch()
    for resource_spans in request.resource_spans:
        resource = {kv.key: _pb_value(kv.value) for kv in resource_spans.resource.attributes}
        for scope_spans in resource_spans.scope_spans:
            for s in scope_spans.spans:
                try:
                    batch.spans.append(
                        _checked(
                            RawSpan(
                                trace_id=s.trace_id.hex(),
                                span_id=s.span_id.hex(),
                                parent_span_id=s.parent_span_id.hex() or None,
                                name=s.name,
                                kind=int(s.kind),
                                start=_time(s.start_time_unix_nano),
                                end=_time(s.end_time_unix_nano),
                                status=STATUS.get(int(s.status.code), "unset"),
                                status_message=s.status.message,
                                attributes={kv.key: _pb_value(kv.value) for kv in s.attributes},
                                resource=resource,
                                events=[
                                    {"name": e.name, "attributes": {kv.key: _pb_value(kv.value) for kv in e.attributes}}
                                    for e in s.events
                                ],
                            )  # fmt: skip
                        )
                    )
                except SpanRejected as exc:
                    batch.reject(str(exc))
                except SPAN_ERRORS:
                    batch.reject("A span has a value that cannot be read.")
    return batch


def _attrs(items: list | None) -> dict:
    return {kv["key"]: _json_value(kv.get("value", {})) for kv in items or []}


def _json_span(s: dict, resource: dict) -> RawSpan:
    status = s.get("status", {})
    return RawSpan(
        trace_id=str(s["traceId"]).lower(),
        span_id=str(s["spanId"]).lower(),
        parent_span_id=str(s.get("parentSpanId") or "").lower() or None,
        name=str(s.get("name", "")),
        kind=int(s.get("kind", 0)) if not isinstance(s.get("kind"), str) else 0,
        start=_time(s["startTimeUnixNano"]),
        end=_time(s["endTimeUnixNano"]),
        status=STATUS.get(status.get("code", 0), "unset"),
        status_message=str(status.get("message", "")),
        attributes=_attrs(s.get("attributes")),
        resource=resource,
        events=[{"name": e.get("name", ""), "attributes": _attrs(e.get("attributes"))} for e in s.get("events", [])],
    )  # fmt: skip


def _from_json(body: bytes) -> ParsedBatch:
    try:
        data = json.loads(body)
        resource_blocks = data.get("resourceSpans", [])
        if not isinstance(resource_blocks, list):
            raise TypeError("resourceSpans must be a list")
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidPayload("The body is not a valid JSON trace export.") from exc
    batch = ParsedBatch()
    for resource_spans in resource_blocks:
        try:
            resource = _attrs(resource_spans.get("resource", {}).get("attributes"))
            scopes = resource_spans.get("scopeSpans", [])
        except SPAN_ERRORS as exc:
            raise InvalidPayload("The body is not a valid JSON trace export.") from exc
        for scope_spans in scopes:
            for s in scope_spans.get("spans", []):
                try:
                    batch.spans.append(_checked(_json_span(s, resource)))
                except SpanRejected as exc:
                    batch.reject(str(exc))
                except SPAN_ERRORS:
                    batch.reject("A span is missing a required field or has a value that cannot be read.")
    return batch


def parse_request(body: bytes, content_type: str) -> ParsedBatch:
    """Valid spans, and a count of the spans that were dropped and why. Only a malformed body raises."""
    kind = content_type.split(";")[0].strip().lower()
    if kind == PROTOBUF:
        batch = _from_protobuf(body)
    elif kind == JSON:
        batch = _from_json(body)
    else:
        raise InvalidPayload("Content-Type must be application/x-protobuf or application/json.")
    if len(batch.spans) + batch.rejected > MAX_SPANS_PER_BATCH:
        raise InvalidPayload(f"A batch may hold at most {MAX_SPANS_PER_BATCH} spans.")
    return batch
