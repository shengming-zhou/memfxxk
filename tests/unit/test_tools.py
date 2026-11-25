from datetime import UTC, datetime
from uuid import UUID

from jsonschema import Draft202012Validator

from memfxxk.domain import Scope
from memfxxk.domain.retrieval import (
    ScoreDetails,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SearchTimings,
)
from memfxxk.tools import dispatch, to_anthropic_tools, to_openai_tools


class _Backend:
    def search(self, request: SearchRequest) -> SearchResponse:
        now = datetime.now(UTC)
        hit = SearchHit(
            memory_id=UUID(int=1),
            version=1,
            type="note",
            event_time=now,
            recorded_at=now,
            scope=Scope(org_id="test"),
            snippet="content " * 100,
            content_truncated=True,
            marks=[],
            score=ScoreDetails(rrf=1, boost=1, final=1),
        )
        return SearchResponse(hits=[hit] * 5, query_tokens=[], timings_ms=SearchTimings())


def test_tool_specs_and_parameter_validation() -> None:
    openai = to_openai_tools()
    anthropic = to_anthropic_tools()
    assert len(openai) == len(anthropic) == 3
    for platform_a, platform_b in zip(openai, anthropic, strict=True):
        function = platform_a["function"]
        assert function["name"] == platform_b["name"]
        assert function["parameters"] == platform_b["input_schema"]
        Draft202012Validator.check_schema(function["parameters"])
    backend = _Backend()
    result = dispatch(backend, "memory_search", {"query": "a test", "k": 0})
    assert result.startswith("Error: k:")
    assert "\n" not in result


def test_tool_output_is_bounded() -> None:
    result = dispatch(
        _Backend(),
        "memory_search",
        {"query": "test"},
        max_chars=125,
        max_item_chars=55,
    )
    assert len(result) <= 125
    assert all(len(line) <= 55 for line in result.splitlines())
