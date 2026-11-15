"""Scripted behaviour for LLM_PROVIDER=stub, so the Investigator runs end to end without a model.

It looks at the most recent day of the first project and reports its error rate. Its report says it is a script.
"""

import re
from datetime import UTC, datetime, timedelta

from app.llm.stub import reply
from app.llm.types import LLMResponse, Message, ToolCall, ToolSpec


def demo_responder(messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
    step = sum(len(m.tool_calls) for m in messages if m.role == "assistant")
    first = next((m.content for m in messages if m.role == "user"), "")
    project = (re.findall(r"projects: ([^,.]+)", first) or ["none"])[0]
    handles = re.findall(r"ev_[0-9a-f]{8}", " ".join(m.content for m in messages if m.role == "tool"))
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    def call(name, **arguments):
        return reply(tool_calls=[ToolCall(f"stub_{step}", name, arguments)])

    if "alert me" in first.lower() or "roll" in first.lower() and "back" in first.lower():
        if step == 0:
            return call(
                "propose_rollback",
                project=project,
                prompt="order-lookup",
                to_version=6,
                rationale="Requested in the question (demonstration script).",
            )
        return call(
            "finish",
            summary="This is the built-in demonstration script, not a language model. The rollback request was handled.",
        )
    if step == 0:
        return call("query_metrics", project=project, start=(end - timedelta(days=1)).isoformat(), end=end.isoformat())
    if step == 1 and handles:
        return call(
            "record_finding",
            claim=f"Calls and errors for {project} over the last day were read from the roll-ups.",
            evidence=handles[:1],
        )
    return call(
        "finish",
        summary="This report was produced by the built-in demonstration script, not by a language model. Open the evidence for the figures.",
        findings=[1] if handles else [],
    )
