import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, tuple_

from memfxxk.db import records
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.retrieval import SearchHit, SearchRequest
from memfxxk.llm.runner import TaskRunner
from memfxxk.prompts import rerank
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService


@dataclass(frozen=True, slots=True)
class ModelReranker:
    service: "BoundService"

    def rerank(self, request: SearchRequest, hits: list[SearchHit]) -> list[SearchHit]:
        shortlist = hits[: self.service.settings.search.rerank_candidates]
        if len(shortlist) < 2:
            return hits
        with self.service.engine.connect() as conn:
            sources = {
                source
                for (source_ids,) in conn.execute(
                    select(records.c.source_event_ids).where(
                        visible_filter(records, self.service.ctx),
                        tuple_(records.c.memory_id, records.c.version).in_(
                            [(hit.memory_id, hit.version) for hit in shortlist]
                        ),
                    )
                )
                for source in source_ids
            }
        sources.update(
            key for hit in shortlist if hit.graph_fact for key in hit.graph_fact.source_event_ids
        )
        payload = {
            "query": request.query,
            "passages": [
                {"index": index, "text": hit.snippet, "time": hit.event_time.isoformat()}
                for index, hit in enumerate(shortlist)
            ],
        }
        try:
            parsed = (
                TaskRunner.for_service(self.service, "rerank")
                .complete(
                    "rerank",
                    self.service.ctx,
                    [
                        {"role": "system", "content": rerank.PROMPT},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    schema=rerank.SCHEMA,
                    prompt_version=rerank.VERSION,
                    source_event_ids=sorted(sources),
                )
                .parsed
            )
        except ServiceError:
            return hits
        if parsed is None:
            return hits
        order = list(dict.fromkeys(i for i in parsed.order if 0 <= i < len(shortlist)))
        for rank, index in enumerate(order, 1):
            shortlist[index].score.rerank_position = rank
        chosen = set(order)
        return [
            *[shortlist[index] for index in order],
            *[hit for index, hit in enumerate(hits) if index not in chosen],
        ]
