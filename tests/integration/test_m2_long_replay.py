import json
import re
from collections import Counter
from html import escape
from time import perf_counter
from uuid import UUID

import pytest

from memfxxk.domain import Principal
from memfxxk.domain.context import BuildContextRequest, PrepareCompaction
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.eval.longmemeval.dataset import DATA_DIRECTORY, DATA_FILES, iter_questions, iter_turns
from memfxxk.jobs.worker import drain
from memfxxk.text import make_token_counter

pytestmark = [pytest.mark.integration, pytest.mark.slow]
_CATEGORIES = ("knowledge-update", "temporal-reasoning")
_PER_CATEGORY = 4
_CITED_EVENT = re.compile(r"\[([a-f0-9-]{36})\]")


def test_longmemeval_s_context_replay(service, make_ctx):
    path = DATA_DIRECTORY / DATA_FILES["s"]
    if not path.exists():
        pytest.skip("Download LongMemEval S before running the replay")
    counter = make_token_counter(service.settings.token_counter)
    counts = Counter()
    rows = []
    for question in iter_questions(path):
        category = question.question_type
        if category not in _CATEGORIES or question.abstention or counts[category] == _PER_CATEGORY:
            continue
        ctx = make_ctx(user="long-replay", project=question.question_id)
        bound = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
        # Replay all supplied sessions as one chronological host conversation.
        histories = sorted(iter_turns(question), key=lambda turn: turn.event_time)
        batch, gold_positions = [], set()
        for turn in histories:
            for message in turn.messages:
                if message.has_answer:
                    gold_positions.add(len(batch))
                batch.append(
                    IngestEvent(
                        kind="message",
                        role=message.role,
                        content=message.content,
                        session_id=question.question_id,
                        turn_id=turn.turn_id,
                        event_time=turn.event_time,
                    )
                )
        if not gold_positions:
            continue
        cutoff = max(
            (
                i + 1
                for i, event in enumerate(batch[: 3 * len(batch) // 4])
                if event.role == "assistant"
            ),
            default=0,
        )
        if not cutoff:
            continue
        started = perf_counter()
        stored = bound.ingest(batch)
        gold = {stored[index].event_id for index in gold_positions}
        gold_turns = {
            memory_id for index in gold_positions for memory_id in stored[index].memory_ids_touched
        }
        request = BuildContextRequest(
            session_id=question.question_id,
            max_tokens=512,
            sections=["working"],
            budget={"working": 512},
            auto_recall=False,
        )
        tail = bound.build_context(request)
        drain(service, kinds={"embed_record"})
        candidates = bound.search(SearchRequest(query=question.question, k=8, types=["turn"]))
        evidence_request = BuildContextRequest(
            query=question.question,
            max_tokens=512,
            sections=["evidence"],
            budget={"evidence": 512},
        )
        hybrid = bound.build_context(evidence_request)
        excerpt = bound.build_context(
            evidence_request.model_copy(update={"evidence_mode": "excerpt"})
        )
        state = bound.get_session(question.question_id)
        draft = bound.prepare_compaction(
            PrepareCompaction(
                session_id=question.question_id,
                revision=state.revision,
                through_seq=cutoff,
                target_tokens=160,
            )
        )
        bound.commit_compaction(draft.draft_id)
        rolling = bound.build_context(request)

        def gold_in(bundle):
            ids = {
                UUID(item.ref_id)
                for item in bundle.sections[0].items
                if item.kind == "event" and not item.truncated
            }
            return len(gold & ids)

        tail_hits = gold_in(tail)
        rolling_hits = gold_in(rolling)
        if any(item.reason == "rolling_summary" for item in rolling.sections[0].items):
            # Count an excerpt citation only if its line also contains the complete gold message.
            by_id = {stored[index].event_id: batch[index].content for index in gold_positions}
            rolling_hits += sum(
                by_id.get(UUID(match[1])) in line
                for line in draft.summary.splitlines()
                if (match := _CITED_EVENT.match(line)) and UUID(match[1]) in gold
            )
        counts[category] += 1

        def gold_in_evidence(bundle):
            return sum(
                escape(batch[index].content or "", quote=False) in bundle.rendered
                for index in gold_positions
            )

        rows.append(
            {
                "question_id": question.question_id,
                "type": category,
                "input_tokens": sum(counter.count(event.content or "") for event in batch),
                "gold_events": len(gold),
                "tail_gold": tail_hits,
                "rolling_gold": rolling_hits,
                "hybrid_candidates": len(gold_turns & {hit.memory_id for hit in candidates.hits}),
                "hybrid_loaded": len(
                    gold_turns & {UUID(item.ref_id) for item in hybrid.sections[0].items}
                ),
                "excerpt_loaded": len(
                    gold_turns & {UUID(item.ref_id) for item in excerpt.sections[0].items}
                ),
                "hybrid_gold": gold_in_evidence(hybrid),
                "excerpt_gold": gold_in_evidence(excerpt),
                "tail_tokens": tail.total_tokens,
                "rolling_tokens": rolling.total_tokens,
                "replay_ms": round((perf_counter() - started) * 1000, 1),
            }
        )
        assert max(b.total_tokens for b in (tail, rolling, hybrid, excerpt)) <= request.max_tokens
        if all(counts[kind] == _PER_CATEGORY for kind in _CATEGORIES):
            break
    print("LongMemEval S replay", json.dumps(rows))
    assert dict(counts) == dict.fromkeys(_CATEGORIES, _PER_CATEGORY)
