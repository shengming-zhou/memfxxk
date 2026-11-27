from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from memfxxk.context import _Candidate, _evidence, _pinned
from memfxxk.context.adaptive_sources import _target, _units
from memfxxk.context.feedback import reference
from memfxxk.context.priorities import effective
from memfxxk.context.resolutions import fallback, fit
from memfxxk.domain.consistency import (
    ChapterOutline,
    ConsistencyConflict,
    ConsistencyRequest,
    ConsistencyResult,
    OutlineRequest,
    OutlineResult,
    OutlineSection,
)
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.resolutions import ResolutionTarget, TextResolution
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2.compaction import _current
from memfxxk.prompts import consistency_check
from memfxxk.sessions import read_session
from memfxxk.text import MixedTokenizer, make_token_counter

if TYPE_CHECKING:
    from memfxxk.service import BoundService


@dataclass(frozen=True, slots=True)
class Evidence:
    item: _Candidate
    priority: int
    relevance: float


def _evidence_for(service: BoundService, request: ConsistencyRequest) -> list[Evidence]:
    search = BuildContextRequest(
        query=request.text[:2400],
        k=32,
        search_filters=SearchFilters(graph_hops=request.graph_hops),
    )
    tokenizer = MixedTokenizer(
        stop_words={"the", "a", "an", "of", "was", "in", "it", "to", "at", "is", "and"}
    )
    terms = set(tokenizer.tokenize(request.text))
    with service.engine.connect() as conn:
        results, _ = _evidence(conn, service, search, set(), None)
        units, targets, _ = _units(
            conn, service, results, terms, tokenizer, False, request.text, all_units=True
        )
        evidence = []
        seen = set()
        for index, item in enumerate([*units, *_pinned(conn, service), *results]):
            if (
                item.excluded_reason
                or not item.content
                or (item.row is not None and item.row["type"] == "doc_chunk")
            ):
                continue
            identity = (reference(item.ref), item.ref.graph_edge_id)
            if identity in seen:
                continue
            seen.add(identity)
            target = targets[index] if index in targets else _target(item)
            priority = effective(conn, service.ctx, target).priority
            overlap = terms.intersection(tokenizer.tokenize(item.content))
            score = max(0.0, float(item.ref.score or 0.0)) if overlap else 0.0
            if item.ref.kind == "document" and item.ref.score == 1.0:
                score = 1.0
            relevance = max(score, min(1.0, len(overlap) / 5))
            evidence.append(Evidence(item, priority, relevance))
    return evidence


def _offset(text: str, excerpt: str, proposed: int | None) -> int | None:
    if proposed is not None and text[proposed : proposed + len(excerpt)] == excerpt:
        return proposed
    start = text.find(excerpt)
    return start if start >= 0 and text.find(excerpt, start + 1) < 0 else None


def check(service: BoundService, request: ConsistencyRequest) -> ConsistencyResult:
    items = _evidence_for(service, request)
    levels = sorted({item.priority for item in items}, reverse=True)
    minimum = request.min_priority
    if minimum is None and levels:
        minimum = levels[min(1, len(levels) - 1)]
    if minimum is None:
        return ConsistencyResult(conflicts=[], checked=[])
    evidence = sorted(
        (item for item in items if item.relevance > 0 and item.priority >= minimum),
        key=lambda item: (-item.relevance, -item.priority, item.item.ref.ref_id),
    )
    counter = make_token_counter(service.settings.token_counter)
    used = 0
    packed = {}
    for entry in evidence[:16]:
        cap = min(600, request.evidence_tokens - used)
        if cap < 16:
            break
        text = fit(entry.item.content, cap, counter)
        used += counter.count(text)
        packed[f"e{len(packed)}"] = (entry.item, text)
    targets = [reference(item.ref) for item, _ in packed.values()]
    if not packed:
        return ConsistencyResult(conflicts=[], checked=[], min_priority=minimum)
    result = TaskRunner.for_service(service, "consistency_check").complete(
        "consistency_check",
        service.ctx,
        [
            {"role": "system", "content": consistency_check.PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "draft": request.text,
                        "sources": {key: text for key, (_, text) in packed.items()},
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        schema=consistency_check.SCHEMA,
        prompt_version=consistency_check.VERSION,
        max_tokens=1000,
    )
    conflicts = []
    seen = set()
    assert result.parsed is not None
    for finding in result.parsed.conflicts:
        if finding.source not in packed:
            continue
        item, text = packed[finding.source]
        start = _offset(request.text, finding.excerpt, finding.start)
        if start is None or finding.source_excerpt not in text:
            continue
        end = start + len(finding.excerpt)
        key = (finding.source, start, end)
        if key in seen:
            continue
        seen.add(key)
        conflicts.append(
            ConsistencyConflict(
                target=reference(item.ref),
                version=item.ref.version,
                revision=item.ref.revision,
                graph_edge_id=item.ref.graph_edge_id,
                start=start,
                end=end,
                excerpt=finding.excerpt,
                source_excerpt=finding.source_excerpt,
                explanation=finding.explanation,
            )
        )
    return ConsistencyResult(
        conflicts=conflicts, checked=targets, min_priority=minimum, llm_call_id=result.call_id
    )


def outline(service: BoundService, request: OutlineRequest) -> OutlineResult:
    counter = make_token_counter(service.settings.token_counter)
    chapters = []
    lines = []
    checkpoint_ids = []
    for path in dict.fromkeys(request.paths):
        document = service.get_resolutions(ResolutionTarget(kind="document", path=path))
        chapters.append(
            ChapterOutline(
                id=document.id,
                generation=document.generation,
                sections=[
                    OutlineSection.model_validate(section.model_dump(exclude={"full"}))
                    for section in document.sections
                ],
            )
        )
        lines.extend(section.summary.text for section in document.sections)
    if request.session_ids:
        with service.engine.connect() as conn:
            for session_id in dict.fromkeys(request.session_ids):
                session = read_session(conn, service.ctx, session_id)
                current = _current(conn, service, session_id) if session.summary_upto_seq else None
                if current is not None:
                    checkpoint_ids.append(current["memory_id"])
                    lines.append(current["content"])
                elif session.summary:
                    lines.append(session.summary)
    per_unit = max(1, request.max_tokens // max(1, len(lines)))
    combined = "\n".join(fit(line, per_unit, counter) for line in lines)
    summary = fit(combined, request.max_tokens, counter)
    line = fallback(combined, counter).line
    return OutlineResult(
        chapters=chapters,
        checkpoint_ids=checkpoint_ids,
        summary=TextResolution(
            text=summary, tokens=counter.count(summary), method="extractive_fallback"
        ),
        line=line,
    )
