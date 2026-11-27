"""Exercise the production consistency check on three violations and three clean drafts."""

import argparse
import json
from pathlib import Path
from time import perf_counter

from run_m2_paired import _isolated_service, _write
from run_t17_writing import SUITE, original
from sqlalchemy import select

from memfxxk.config import Settings
from memfxxk.db import llm_calls
from memfxxk.domain.consistency import ConsistencyRequest
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.story import LoreKeys
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.prompts.consistency_check import VERSION
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    config = settings.task_model("consistency_check")
    if not config.model:
        parser.error("Configure the explicit consistency_check task")
    cases = json.loads(SUITE.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    outcome = []
    with _isolated_service(settings) as isolated:
        service = MemoryService(
            isolated.engine, isolated.settings.model_copy(update={"llm": settings.llm})
        )
        bound = service.for_request(EVAL_PRINCIPAL, eval_context("t17-consistency-smoke"))
        by_id = {}
        for doc in cases["documents"]:
            saved = bound.put_document(
                doc["path"],
                PutDocumentRequest(
                    content=original(doc),
                    load_policy=doc["policy"],
                    lore={doc["heading"]: LoreKeys(keywords=doc["keys"])},
                ),
            )
            by_id[str(saved.document_id)] = doc["path"]
        for case in (
            item
            for item in cases["cases"]
            if item["id"]
            in {
                "c01",
                "c03",
                "c05",
                "c06",
                "c07",
                "c08",
            }
        ):
            started = perf_counter()
            result = bound.check_consistency(ConsistencyRequest(text=case["text"]))
            elapsed = (perf_counter() - started) * 1000
            matches = [
                conflict
                for conflict in result.conflicts
                if by_id.get(conflict.target.ref_id) in case["sources"]
                and case["draft_span"]
                and case["draft_span"] in conflict.excerpt
            ]
            relative_scope = bound.ctx
            with service.engine.connect() as conn:
                usage = (
                    conn.execute(
                        select(llm_calls).where(
                            visible_filter(llm_calls, relative_scope),
                            llm_calls.c.id == result.llm_call_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            outcome.append(
                {
                    "case": case["id"],
                    "correct": bool(matches) if case["draft_span"] else not result.conflicts,
                    "conflicts": len(result.conflicts),
                    "sources": len(result.checked),
                    "elapsed_ms": round(elapsed, 2),
                    "input_tokens": usage["input_tokens"] if usage else 0,
                    "output_tokens": usage["output_tokens"] if usage else 0,
                }
            )
    _write(
        args.output / "metrics.json", {"model": config.model, "prompt": VERSION, "cases": outcome}
    )


if __name__ == "__main__":
    main()
