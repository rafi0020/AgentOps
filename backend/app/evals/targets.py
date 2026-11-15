"""What an experiment runs each dataset item against: a prompt version with a model, or an HTTP endpoint.

HTTP targets are a server-side request made on a user's say-so, so where they may point is restricted, and
the address that was checked is the address that is connected to. Resolving the name again at connect time
would let a hostname answer "public" to the check and "internal" to the connection.
"""

import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.llm.types import Message
from app.prompts.registry import render
from app.security.crypto import decrypt, encrypt

MAX_RESPONSE_BYTES = 1024 * 1024
TIMEOUT = httpx.Timeout(connect=10, read=60, write=30, pool=10)


class TargetError(Exception):
    """The target could not produce an output for this item. The message is shown in the result."""


@dataclass
class ResolvedTarget:
    scheme: str
    host: str
    ip: str
    port: int
    path: str


@dataclass
class TargetOutput:
    output: object
    latency_ms: float
    cost_usd: Decimal


def validate_target_url(url: str, resolve=socket.getaddrinfo) -> ResolvedTarget:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("The target URL must start with http:// or https:// and include a host.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = [info[4][0] for info in resolve(parsed.hostname, port, type=socket.SOCK_STREAM)]
    except (socket.gaierror, OSError) as exc:
        raise ValueError("The target host cannot be resolved.") from exc
    allow_private = get_settings().allow_private_targets
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("The target URL points to an address that is not allowed.")  # includes cloud metadata
        if not allow_private and not ip.is_global:
            raise ValueError("Targets on private or local networks are disabled on this installation.")
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    return ResolvedTarget(parsed.scheme, parsed.hostname, addresses[0], port, path)


def _dig(value, path: str):
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise TargetError(f"The response has no '{path}'.")
    return value


def seal_target(target: dict) -> dict:
    """Headers can hold credentials, so they are stored encrypted, the way provider keys are."""
    headers = target.pop("headers", None) or {}
    if headers:
        target["headers_enc"] = encrypt(json.dumps(headers, sort_keys=True))
    return target


def request_headers(target: dict) -> dict[str, str]:
    """The headers to send. Rows written before sealing kept them in plain text under `headers`."""
    if target.get("headers_enc"):
        return {str(k): str(v) for k, v in json.loads(decrypt(target["headers_enc"])).items()}
    return {str(k): str(v) for k, v in (target.get("headers") or {}).items()}


def public_target(target: dict) -> dict:
    """The target as the API shows it: header values never leave the server."""
    shown = {k: v for k, v in target.items() if k != "headers_enc"}
    if target.get("headers_enc") or target.get("headers"):
        shown["headers"] = "[redacted]"
    return shown


async def call_http(
    target: dict, item_input, *, resolve=socket.getaddrinfo, transport: httpx.AsyncBaseTransport | None = None
) -> TargetOutput:
    try:
        resolved = validate_target_url(target["url"], resolve)  # checked once, here
    except ValueError as exc:
        raise TargetError(str(exc)) from exc
    host = f"[{resolved.ip}]" if ":" in resolved.ip else resolved.ip
    url = f"{resolved.scheme}://{host}:{resolved.port}{resolved.path}"  # connect to the address that was checked
    headers = request_headers(target) | {"Host": resolved.host}
    body = target.get("body_template")
    payload = (
        json.loads(json.dumps(body).replace('"{{input}}"', json.dumps(item_input)))
        if body is not None
        else {"input": item_input}
    )
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport, follow_redirects=False) as client:
            request = client.build_request(
                target.get("method", "POST"),
                url,
                headers=headers,
                json=payload,
                extensions={"sni_hostname": resolved.host},
            )
            response = await client.send(request, stream=True)
            try:
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise TargetError("The target's response is larger than 1 MB.")
                    chunks.append(chunk)
            finally:
                await response.aclose()
    except httpx.HTTPError as exc:
        raise TargetError(f"The target could not be reached: {type(exc).__name__}.") from exc
    latency = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        raise TargetError(f"The target returned HTTP {response.status_code}.")
    text = b"".join(chunks).decode("utf-8", errors="replace")
    output: object = text
    if target.get("output_path"):
        try:
            output = _dig(json.loads(text), target["output_path"])
        except json.JSONDecodeError as exc:
            raise TargetError("The target did not return JSON, so output_path cannot be applied.") from exc
    return TargetOutput(output, latency, Decimal(0))


async def call_prompt(client, version_text: str, model_settings: dict, item_input) -> TargetOutput:
    variables = item_input if isinstance(item_input, dict) else {"input": item_input}
    try:
        prompt = render(version_text, variables)
    except ValueError as exc:
        raise TargetError(str(exc)) from exc
    started = time.perf_counter()
    response, cost = await client.complete(
        [Message("user", prompt)], [], max_tokens=int(model_settings.get("max_tokens", 1024))
    )
    return TargetOutput(response.text, (time.perf_counter() - started) * 1000, cost)
