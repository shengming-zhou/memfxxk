"""Check six adaptive requests without running the full writing comparison."""

import argparse
from pathlib import Path
from time import perf_counter

from run_m2_paired import _isolated_service, _write
from sqlalchemy import select

from memfxxk.config import Settings
from memfxxk.db import llm_calls
from memfxxk.domain.context import BuildContextRequest
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.priorities import PriorityTarget, SetPriority
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

CHAPTERS = {
    "Gates": "A scarlet flare closes the north gate.",
    "Ferry": "No boats cross during a winter storm.",
    "Lanterns": "Brass lanterns identify the southern watch.",
}
QUESTIONS = (
    ("What does a scarlet flare do?", 120),
    ("When do boats stop crossing?", 120),
    ("How are the southern watch identified?", 120),
    ("Explain all three rules.", 160),
    ("Describe the watch during a storm.", 80),
    ("List emergency entry rules.", 44),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if args.live and not settings.task_model("context_plan").model:
        parser.error("Configure the explicit context_plan task")
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    with _isolated_service(settings) as isolated:
        service = MemoryService(
            isolated.engine, isolated.settings.model_copy(update={"llm": settings.llm})
        )
        for position, (query, budget) in enumerate(QUESTIONS, 1):
            scope = eval_context(f"t17-p12:{position}")
            bound = service.for_request(EVAL_PRINCIPAL, scope)
            for priority, (heading, rule) in enumerate(CHAPTERS.items(), 1):
                path = f"world/{heading.lower()}.md"
                original = f"# {heading}\n{rule}\n\n" + "The stones are large and grey. " * 20
                bound.put_document(path, PutDocumentRequest(content=original, load_policy="always"))
                bound.set_priority(
                    SetPriority(
                        target=PriorityTarget(kind="document", path=path), priority=priority
                    )
                )
            start = perf_counter()
            bundle = bound.build_context(
                BuildContextRequest(
                    query=query,
                    allocator="adaptive",
                    max_tokens=budget,
                    plan_with_llm=args.live,
                    auto_recall=False,
                )
            )
            elapsed = (perf_counter() - start) * 1000
            assert bundle.allocation is not None
            assert bundle.total_tokens <= budget
            with service.engine.connect() as connection:
                calls = list(
                    connection.execute(
                        select(llm_calls).where(visible_filter(llm_calls, scope))
                    ).mappings()
                )
            rows.append(
                {
                    "case": position,
                    "budget": budget,
                    "tokens": bundle.total_tokens,
                    "elapsed_ms": round(elapsed, 2),
                    "included": [key for key, rule in CHAPTERS.items() if rule in bundle.rendered],
                    "coefficients": bundle.allocation.plan_coefficients,
                    "calls": len(calls),
                    "input_tokens": sum(call["input_tokens"] for call in calls),
                    "output_tokens": sum(call["output_tokens"] for call in calls),
                    "errors": [call["error_code"] for call in calls if call["error_code"]],
                }
            )
    _write(
        args.output / "metrics.json",
        {
            "live": args.live,
            "cases": rows,
            "model": (settings.task_model("context_plan").model if args.live else None),
        },
    )


if __name__ == "__main__":
    main()
