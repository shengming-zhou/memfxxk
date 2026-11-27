from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter
from sqlalchemy import select

from memfxxk.config import M2Settings, Settings
from memfxxk.db import fact_proposals, jobs, llm_calls
from memfxxk.domain import Principal
from memfxxk.domain.events import IngestEvent
from memfxxk.jobs import Worker
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import BudgetExceeded, FakeLLM, TaskRunner
from memfxxk.llm.settings import DailyLimit, LLMSettings, TaskModel
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def _settings(base: Settings, **options: object) -> Settings:
    return base.model_copy(
        update={
            "llm": LLMSettings(
                default=TaskModel(model="fixture", max_retries=0, max_output_tokens=32),
                **options,
            )
        }
    )


def test_task_retries_validation_and_scopes_audit(engine, test_settings, make_ctx) -> None:
    owner = make_ctx(user="alice", project="atlas")
    other = make_ctx(user="bob", project="atlas")
    upstream = FakeLLM(
        {
            "compact": [
                ChatResult({"content": "broken"}),
                ChatResult({"content": '{"count": 3}'}, 8, 4),
            ]
        }
    )
    service = MemoryService(engine, _settings(test_settings), upstream)
    result = TaskRunner.for_service(service, "compact").complete(
        "compact",
        owner,
        [{"role": "user", "content": "private text"}],
        schema=TypeAdapter(dict[str, int]),
        prompt_version="test-v1",
    )
    assert result.parsed == {"count": 3}
    assert "Validation errors" in upstream.calls[-1][2][-1]["content"]
    with engine.connect() as conn:
        rows = conn.execute(
            select(llm_calls).where(visible_filter(llm_calls, owner)).order_by(llm_calls.c.id)
        ).all()
        assert [row.status for row in rows] == ["failed", "succeeded"]
        assert rows[0].request_id == rows[1].request_id
        assert rows[0].input_sha256 != rows[1].input_sha256
        assert (rows[1].input_tokens, rows[1].output_tokens, rows[1].retries) == (8, 4, 1)
        assert not any(row.debug_input or row.debug_output for row in rows)
        assert not conn.execute(select(llm_calls).where(visible_filter(llm_calls, other))).all()


def test_org_budget_rejects_sync_and_defers_worker(engine, test_settings, make_ctx) -> None:
    ctx = make_ctx(user="alice", project="atlas")
    denied = DailyLimit(org_id=ctx.org_id, tokens=0)
    settings = _settings(test_settings, daily_limits=[denied]).model_copy(
        update={
            "m2": M2Settings(extract_facts=True),
        }
    )
    fake = FakeLLM({})
    service = MemoryService(engine, settings, fake)
    with pytest.raises(BudgetExceeded) as failure:
        TaskRunner.for_service(service, "compact").complete(
            "compact",
            ctx,
            [{"role": "user", "content": "Text"}],
            prompt_version="v1",
        )
    assert failure.value.status_code == 429
    owner = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
    owner.ingest(
        [
            IngestEvent(kind="message", role="user", session_id="s", turn_id="1", content="a"),
            IngestEvent(kind="message", role="assistant", session_id="s", turn_id="1", content="b"),
        ]
    )
    assert Worker(service).run_once(["extract_facts"]) == 1
    with engine.connect() as conn:
        job = conn.execute(select(jobs).where(jobs.c.kind == "extract_facts")).one()
        assert (job.status, job.attempts, job.last_error) == ("queued", 0, "llm_budget_exceeded")
        assert job.run_after > datetime.now(UTC)
        assert conn.scalars(select(llm_calls.c.status)).all() == ["budget_exceeded"] * 2
    assert not fake.calls


def test_failed_structured_extraction_and_debug_erasure(engine, test_settings, make_ctx) -> None:
    ctx = make_ctx(user="alice", project="atlas")
    settings = _settings(test_settings, debug_payloads=True).model_copy(
        update={
            "m2": M2Settings(extract_facts=True),
        }
    )
    upstream = FakeLLM({"extract": [ChatResult({"content": "no"}), ChatResult({"content": "bad"})]})
    service = MemoryService(engine, settings, upstream)
    owner = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
    first, _ = owner.ingest(
        [
            IngestEvent(kind="message", role="user", session_id="s", turn_id="1", content="a"),
            IngestEvent(kind="message", role="assistant", session_id="s", turn_id="1", content="b"),
        ]
    )
    assert Worker(service).run_once(["extract_facts"]) == 1
    with engine.connect() as conn:
        assert not conn.execute(select(fact_proposals)).all()
        calls = conn.execute(select(llm_calls)).all()
        assert len(calls) == 2 and all(call.debug_input and call.debug_output for call in calls)
    owner.forget_event(first.event_id)
    with engine.connect() as conn:
        calls = conn.execute(select(llm_calls)).all()
        assert len(calls) == 2 and all(
            call.debug_input is None and call.debug_output is None for call in calls
        )
