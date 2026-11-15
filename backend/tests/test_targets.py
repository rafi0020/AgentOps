"""Where an experiment's HTTP target may point, and that the checked address is the one connected to."""

import json

import httpx
import pytest

from app.config import get_settings
from app.evals.targets import TargetError, call_http, validate_target_url


def resolver(*answers):
    """A DNS stub that returns each answer in turn, to imitate a name that changes between look-ups."""
    calls = []

    def resolve(host, port, **kwargs):
        calls.append(host)
        return [(2, 1, 6, "", (answers[min(len(calls), len(answers)) - 1], port))]

    resolve.calls = calls
    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://",
        "http://0.0.0.0/",
    ],
)
def test_addresses_that_are_never_allowed(url):
    with pytest.raises(ValueError):
        validate_target_url(url)


def test_private_addresses_depend_on_the_setting(monkeypatch):
    assert validate_target_url("http://127.0.0.1:9000/run").ip == "127.0.0.1"  # allowed by default, for local apps
    monkeypatch.setattr(get_settings(), "allow_private_targets", False)
    for url in ("http://127.0.0.1:9000/run", "http://10.0.0.5/", "http://192.168.1.10/", "http://100.64.0.1/"):
        with pytest.raises(ValueError, match="private or local"):
            validate_target_url(url)
    assert validate_target_url("https://example.com/x", resolver("93.184.216.34")).ip == "93.184.216.34"


async def test_the_connection_goes_to_the_address_that_was_checked(monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_private_targets", False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["host"], seen["body"] = str(request.url), request.headers["host"], json.loads(request.content)
        return httpx.Response(200, json={"data": {"answer": "shipped"}})

    rebinding = resolver("93.184.216.34", "127.0.0.1")  # public for the check, internal if asked again
    out = await call_http({"url": "http://app.example.com/run?x=1", "output_path": "data.answer"}, "Where is 4417?",
                          resolve=rebinding, transport=httpx.MockTransport(handler))  # fmt: skip
    assert rebinding.calls == ["app.example.com"]  # resolved exactly once
    assert seen["url"] == "http://93.184.216.34/run?x=1" and seen["host"] == "app.example.com"
    assert seen["body"] == {"input": "Where is 4417?"} and out.output == "shipped"


async def test_body_template_error_statuses_and_oversized_responses():
    def echo(request):
        return httpx.Response(200, json=json.loads(request.content))

    out = await call_http({"url": "http://127.0.0.1/x", "body_template": {"messages": [{"role": "user", "content": "{{input}}"}]}, "output_path": "messages.0.content"},
                          {"q": "hi"}, transport=httpx.MockTransport(echo))  # fmt: skip
    assert out.output == {"q": "hi"}
    with pytest.raises(TargetError, match="HTTP 500"):
        await call_http(
            {"url": "http://127.0.0.1/x"}, "q", transport=httpx.MockTransport(lambda r: httpx.Response(500))
        )
    with pytest.raises(TargetError, match="larger than 1 MB"):
        await call_http(
            {"url": "http://127.0.0.1/x"},
            "q",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (2 * 1024 * 1024))),
        )
    with pytest.raises(TargetError, match="no 'data.missing'"):
        await call_http(
            {"url": "http://127.0.0.1/x", "output_path": "data.missing"},
            "q",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {}})),
        )
    with pytest.raises(TargetError, match="not allowed"):
        await call_http({"url": "http://169.254.169.254/"}, "q")
