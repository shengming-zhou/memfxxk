from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection

from memfxxk.context import _Candidate, _document_index, _wrap_document
from memfxxk.context.adaptive import Candidate, Layer, Variant, allocate
from memfxxk.context.feedback import state
from memfxxk.context.priorities import ContentTarget, effective
from memfxxk.context.resolutions import fallback, read_pairs
from memfxxk.documents import document_contents, list_resolved
from memfxxk.domain.context import (
    AllocationTrace,
    BuildContextRequest,
    ContextReference,
    ContextSection,
    SectionName,
)
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.priorities import PriorityTarget
from memfxxk.domain.resolutions import ResolutionPair
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2.doc_chunks import sections
from memfxxk.prompts import context_plan
from memfxxk.text import MixedTokenizer, TokenCounter

if TYPE_CHECKING:
    from memfxxk.service import BoundService


def _units(
    conn: Connection,
    service: BoundService,
    existing: list[_Candidate],
    terms: set[str],
    tokenizer: MixedTokenizer,
    include_index: bool,
) -> tuple[list[_Candidate], dict[int, ContentTarget], dict[int, ResolutionPair]]:
    matches: dict[tuple[str, int, str], float] = {}
    for item in existing:
        if item.row is None or item.row["type"] != "doc_chunk" or item.excluded_reason:
            continue
        pos = item.row["source_pos"]
        key = (pos["document_id"], pos["revision"], pos["heading"])
        matches[key] = max(matches.get(key, 0.0), item.ref.score or 0.0)
    documents = list_resolved(conn, service.ctx)
    raw_documents = document_contents(conn, documents)
    candidates, targets, pairs = [], {}, {}
    for doc in documents:
        original = raw_documents.get(doc.document_id)
        if original is None:
            continue
        wanted = []
        for index, (heading, content) in enumerate(sections(original)):
            matched = (str(doc.document_id), doc.revision, heading)
            lexical = len(terms.intersection(tokenizer.tokenize(f"{doc.path} {content}"))) / max(
                1, len(terms)
            )
            if doc.load_policy == "always" or matched in matches or lexical >= 0.4:
                wanted.append((index, heading, content, max(lexical, matches.get(matched, 0.0))))
        if not wanted:
            continue
        variants = read_pairs(conn, service.ctx, "document", doc.document_id, doc.revision)
        for index, heading, content, score in wanted:
            position = len(candidates)
            targets[position] = ContentTarget(
                PriorityTarget(
                    kind="section" if heading else "document",
                    path=doc.path,
                    heading=heading or None,
                ),
                doc.document_id,
                doc.scope.project_id,
            )
            if variants is not None:
                pairs[position] = variants[index]
            candidates.append(
                _Candidate(
                    ContextReference(
                        kind="document",
                        ref_id=str(doc.document_id),
                        record_type="doc_section",
                        revision=doc.revision,
                        section_index=index,
                        reason="always_document"
                        if doc.load_policy == "always"
                        else "matched_section",
                        score=score,
                    ),
                    "## Persona & conventions" if doc.load_policy == "always" else "## Reference",
                    content,
                    partial(_wrap_document, doc),
                    f"[use memory_get doc:{doc.path} for full text]",
                )
            )
    if include_index:
        for doc in documents:
            if doc.load_policy == "on_demand":
                targets[len(candidates)] = ContentTarget(
                    PriorityTarget(kind="document", path=doc.path),
                    doc.document_id,
                    doc.scope.project_id,
                )
                candidates.append(_document_index(doc))
    return candidates, targets, pairs


def _target(item: _Candidate) -> ContentTarget:
    if item.row is not None:
        pos = item.row["source_pos"] or {}
        inherited = (
            PriorityTarget(
                kind="section" if pos.get("heading") else "document",
                path=pos["path"],
                heading=pos.get("heading") or None,
            )
            if item.row["type"] == "doc_chunk" and pos.get("path") and pos.get("document_id")
            else None
        )
        return ContentTarget(
            PriorityTarget(kind="record", memory_id=UUID(item.ref.ref_id)),
            UUID(pos["document_id"]) if inherited else None,
            item.row["project_id"],
            inherited,
        )
    return ContentTarget(PriorityTarget(kind="project"))


def _versions(
    conn: Connection,
    service: BoundService,
    item: _Candidate,
    pair: ResolutionPair | None,
    counter: TokenCounter,
) -> tuple[tuple[Variant, ...], tuple[Variant, ...]]:
    if not item.content.strip() or item.excluded_reason:
        return (), ()
    if pair is None and item.row is not None and not item.graph and not item.ref.truncated:
        stored = read_pairs(conn, service.ctx, "record", item.row["id"], item.row["generation"])
        pair = stored[0] if stored else None
    elif pair is None and item.ref.kind == "block":
        stored = read_pairs(
            conn, service.ctx, "block", UUID(item.ref.ref_id), item.ref.revision or 0
        )
        pair = stored[0] if stored else None
    pair = pair or fallback(item.content, counter)

    def render(text: str) -> str:
        return f"{item.heading}\n\n{item.wrap(text)}"

    full = render(item.content)
    variants = [Variant("full", full, counter.count(full + "\n\n"))]
    if item.ref.kind != "index":
        for level, version in (("summary", pair.summary), ("line", pair.line)):
            if not version.text:
                continue
            text = render(version.text)
            count = counter.count(text + "\n\n")
            if count <= variants[-1].tokens:
                variants.append(Variant(level, text, count))
    partials = []
    if item.marker:
        for ending in reversed(list(re.finditer(r"\n\s*\n|\n(?= {0,3}#{1,6}\s)", item.content))):
            prefix = item.content[: ending.start()].rstrip()
            if prefix:
                text = render(f"{prefix}\n\n{item.marker}")
                partials.append(Variant("full", text, counter.count(text + "\n\n")))
    return tuple(reversed(variants)), tuple(partials)


def _plan(
    service: BoundService,
    request: BuildContextRequest,
    candidates: list[Candidate],
    call_ids: list[UUID],
    layers: list[Layer],
) -> dict[int, float]:
    settings = service.settings.context.adaptive
    if "context_plan" not in service.settings.llm.tasks:
        return {}
    try:
        result = TaskRunner.for_service(service, "context_plan").complete(
            "context_plan",
            service.ctx,
            [
                {"role": "system", "content": context_plan.PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "query": (request.query or "")[:512],
                            "layers": [
                                {
                                    "priority": layer.priority,
                                    "weight": layer.weight,
                                    "types": sorted(
                                        {
                                            candidates[i].ref.record_type or candidates[i].ref.kind
                                            for i in layer.items
                                        }
                                    ),
                                }
                                for layer in layers[:100]
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            schema=context_plan.SCHEMA,
            prompt_version=context_plan.VERSION,
            max_tokens=512,
        )
    except ServiceError:
        return {}
    call_ids.append(result.call_id)
    if result.parsed is None:
        return {}
    return {
        layer.priority: min(settings.plan_max, max(settings.plan_min, coefficient))
        for layer in layers
        if (coefficient := result.parsed.coefficients.get(layer.priority)) is not None
    }


def assemble(
    conn: Connection,
    service: BoundService,
    request: BuildContextRequest,
    sources: dict[SectionName, list[_Candidate]],
    counter: TokenCounter,
    budget: int,
) -> tuple[list[ContextSection], str, AllocationTrace]:
    tokenizer = MixedTokenizer()
    terms = set(tokenizer.tokenize(request.query or ""))
    units, targets, pairs = (
        _units(
            conn,
            service,
            sources.get("evidence", []),
            terms,
            tokenizer,
            request.include_on_demand_index,
        )
        if "documents" in sources
        else ([], {}, {})
    )
    document_keys = {
        (units[index].ref.ref_id, units[index].ref.revision, target.target.heading or "")
        for index, target in targets.items()
        if units[index].ref.kind == "document"
    }
    pool: list[Candidate] = []
    # Keep one canonical candidate per physical record. Keep duplicate references in the trace.
    seen: set[tuple[str, str, int | None]] = set()
    retrieved_max = max((item.ref.score or 0 for item in sources.get("evidence", [])), default=0)
    for name, items in sources.items():
        for index, item in enumerate([*units, *items] if name == "documents" else items):
            excluded = item.excluded_reason
            identity = (item.ref.kind, item.ref.ref_id, item.ref.section_index)
            if (
                not excluded
                and item.row is not None
                and item.row["type"] == "doc_chunk"
                and not item.graph
            ):
                pos = item.row["source_pos"]
                if (pos["document_id"], pos["revision"], pos["heading"]) in document_keys:
                    excluded = "covered_by_document_section"
            if identity in seen and not item.graph:
                excluded = excluded or "duplicate_candidate"
            if not excluded:
                seen.add(identity)
            target = targets.get(index) if name == "documents" else None
            priority = effective(conn, service.ctx, target or _target(item)).priority
            overlap = len(terms.intersection(tokenizer.tokenize(item.content))) / max(1, len(terms))
            score = item.ref.score or 0.0
            if name == "evidence" and retrieved_max:
                score = score / retrieved_max
            relevance = max(overlap, min(1.0, max(0.0, score)), 0.5 if not terms else 0.0)
            variants, partials = _versions(
                conn,
                service,
                replace(item, excluded_reason=excluded),
                pairs.get(index) if name == "documents" else None,
                counter,
            )
            pool.append(
                Candidate(
                    item.ref,
                    name,
                    priority,
                    relevance,
                    variants,
                    partials,
                    excluded,
                    item.ref.reason == "always_document" or name == "pinned",
                )
            )
    call_ids: list[UUID] = []
    learned = (
        state(conn, service)
        if request.learn_from_feedback and service.ctx.project_id is not None
        else None
    )
    settings = service.settings.context.adaptive
    if learned is not None:
        settings = settings.model_copy(
            update={"decay": learned.decay, "expand_items": learned.expand_items}
        )
    packing = allocate(
        pool,
        budget,
        settings,
        counter,
        multipliers=learned.multipliers if learned else None,
        plan=partial(_plan, service, request, pool, call_ids) if request.plan_with_llm else None,
    )
    if call_ids:
        packing.trace.plan_call_id = call_ids[0]
    if learned is not None:
        packing.trace.feedback_version = learned.version
    items_by_section = defaultdict(list)
    text_by_section = defaultdict(list)
    dropped = defaultdict(list)
    allocated = dict(packing.ordered)
    for sequence, (index, choice) in enumerate(packing.ordered):
        item = pool[index]
        reason = packing.reasons.get(index)
        truncated = (
            item.ref.truncated
            or choice.level != "full"
            or reason == "paragraph_with_source_reference"
        )
        items_by_section[item.section].append(
            item.ref.model_copy(
                update={
                    "decision": "truncated" if truncated else "selected",
                    "decision_reason": reason
                    or ("fits_at_reduced_detail" if truncated else "fits_budget"),
                    "truncated": truncated,
                    "resolution": choice.level,
                    "effective_priority": item.priority,
                    "candidate_tokens": item.variants[-1].tokens,
                    "tokens": choice.tokens,
                    "render_order": sequence,
                }
            )
        )
        text_by_section[item.section].append(choice.text)
    for index, item in enumerate(pool):
        if index not in allocated:
            dropped[item.section].append(
                item.ref.model_copy(
                    update={
                        "decision": "dropped",
                        "decision_reason": item.excluded
                        or packing.reasons.get(index)
                        or ("layer_budget_insufficient" if item.variants else "no_content"),
                        "effective_priority": item.priority,
                        "candidate_tokens": item.variants[-1].tokens if item.variants else 0,
                    }
                )
            )
    return (
        [
            ContextSection(
                name=name,
                budget=budget,
                used_tokens=counter.count(text),
                items=items_by_section[name],
                dropped=dropped[name],
                rendered=text,
            )
            for name in sources
            if (text := "\n\n".join(text_by_section[name])) or dropped[name] or not sources[name]
        ],
        "\n\n".join(choice.text for _, choice in packing.ordered),
        packing.trace,
    )
