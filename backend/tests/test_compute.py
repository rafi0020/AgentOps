"""The `total` calculation: adding columns, and adding the same column across several stored results.

Found on the held-out evaluation: asked for "input plus output tokens" or "all three projects together", the
Investigator had no operation that adds two columns or adds across results, so it declined to give the figure.
"""

import json
from datetime import timedelta

import pytest

from app.agent.runtime import advance
from app.investigator.agent import AGENT
from app.investigator.compute import ComputeError, run_compute
from app.llm.stub import reply
from app.llm.types import ToolCall
from tests.agent_helpers import new_run
from tests.conftest import DEMO_END

TABLES = {
    "ev_a": {"columns": ["project", "input_tokens", "output_tokens", "cost_usd"], "rows": [["checkout", 2589733, 374028, "1.88115"]]},
    "ev_b": {"columns": ["project", "cost_usd"], "rows": [["doc-search", "1.912128"]]},
    "ev_c": {"columns": ["project", "cost_usd"], "rows": [["support", "1.879338"], ["support-2", None]]},
}  # fmt: skip


def test_total_adds_two_columns_of_one_result():
    result = run_compute("total", {"handle": "ev_a", "columns": ["input_tokens", "output_tokens"]}, TABLES.get)
    assert result.values == {"input_tokens": 2589733, "output_tokens": 374028, "total": 2963761}


def test_total_adds_one_column_across_several_results():
    result = run_compute("total", {"handles": ["ev_a", "ev_b", "ev_c"], "columns": ["cost_usd"]}, TABLES.get)
    assert result.values == {"cost_usd": 5.672616, "total": 5.672616}
    assert [r["source"] for r in result.table] == ["ev_a", "ev_b", "ev_c"]
    assert any("missing" in note for note in result.notes)  # the NULL row is reported, not silently counted as 0


def test_total_rejects_a_column_one_result_lacks():
    with pytest.raises(ComputeError) as err:
        run_compute("total", {"handles": ["ev_a", "ev_b"], "columns": ["input_tokens"]}, TABLES.get)
    assert err.value.code == "bad_column"


def test_total_needs_columns_and_a_known_handle():
    with pytest.raises(ComputeError):
        run_compute("total", {"handle": "ev_a"}, TABLES.get)
    with pytest.raises(ComputeError):
        run_compute("total", {"handles": ["ev_nope"], "columns": ["cost_usd"]}, TABLES.get)


async def test_the_investigator_adds_costs_of_two_projects_from_two_results(lumen, stub_llm):
    day = (DEMO_END - timedelta(days=10)).replace(hour=0, minute=0, second=0, microsecond=0)
    window = dict(start=day.isoformat(), end=(day + timedelta(days=1)).isoformat())

    def tool_outputs(messages):
        return [json.loads(m.content) for m in messages if m.role == "tool" and m.content.startswith("{")]

    def responder(messages, tools):
        done = tool_outputs(messages)
        if len(done) < 2:
            project = ["checkout-assistant", "doc-search"][len(done)]
            return reply(tool_calls=[ToolCall(f"c{len(done)}", "query_metrics", dict(project=project, **window))])
        if len(done) == 2:
            handles = [d["handle"] for d in done]
            return reply(
                tool_calls=[
                    ToolCall("c2", "compute", dict(op="total", args={"handles": handles, "columns": ["cost_usd"]}))
                ]
            )
        return reply(tool_calls=[ToolCall("c3", "finish", dict(summary="done"))])

    stub_llm.responder = responder
    run_id = await new_run(lumen, question="Cost of both together?", role="engineer")
    await advance(run_id, AGENT)
    first, second, total = tool_outputs(stub_llm.calls[-1])
    costs = [sum(r["cost_usd"] for r in d["data"]["untrusted_data"]) for d in (first, second)]
    assert total["data"]["values"]["total"] == pytest.approx(sum(costs), abs=1e-6)
    assert total["handle"].startswith("ev_")
