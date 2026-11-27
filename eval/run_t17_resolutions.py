"""Exercise six source types with the live resolution model and an isolated database."""

import argparse
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from run_m2_paired import _isolated_service, _write
from sqlalchemy import select

from memfxxk.config import Settings
from memfxxk.db import llm_calls, transaction
from memfxxk.domain import Scope
from memfxxk.domain.blocks import CreateBlock
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.resolutions import ResolutionTarget
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.jobs.worker import drain
from memfxxk.m2.storage import derived_record
from memfxxk.prompts.resolution_summarize import VERSION
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

CASES = {
    "document": "The north gate must remain shut during a red tide.",
    "block": "Tess can read charts only under blue lantern light.",
    "note": "No crew can dock at Marsh Quay before sunrise.",
    "fact": "The ferry leaves on Thursday after the second bell.",
    "lesson": "Check the ledger before unlocking the west storeroom.",
    "turn": "Lian lends the brass key, and never the iron key.",
}
DETAILS = (
    " The surrounding walls use coarse grey stone. Old cobbles glitter in light rain. "
    "The ledger is bound with blue cloth and stored beside several empty shelves. "
    "There are three decorative shields along the road. The clerk has a stool near a window. "
    "Visitors sometimes wait under the tall oak trees beside the well. "
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    if not settings.task_model("resolution_summarize").model:
        raise ValueError("Configure the explicit resolution_summarize task model")
    output = []
    start = perf_counter()
    args.output.mkdir(parents=True, exist_ok=True)
    with _isolated_service(settings) as base:
        service = MemoryService(base.engine, base.settings.model_copy(update={"llm": settings.llm}))
        fixtures = []
        for kind, detail in CASES.items():
            scope = eval_context(f"t17-p11:{kind}").model_copy(update={"agent_id": "writer"})
            bound = service.for_request(EVAL_PRINCIPAL, scope)
            text = detail + DETAILS
            if kind == "document":
                path = "world/moor.md"
                bound.put_document(
                    path, PutDocumentRequest(content=f"# The Moor\n{text}", load_policy="always")
                )
                target = ResolutionTarget(kind="document", path=path)
            elif kind == "block":
                block = bound.create_block(
                    CreateBlock(
                        label="World",
                        description="A writing reference",
                        owner="agent",
                        value=text,
                    )
                )
                target = ResolutionTarget(kind="block", id=block.id)
            elif kind == "turn":
                event = bound.ingest(
                    [IngestEvent(kind="message", session_id="sample", role="user", content=text)]
                )[0]
                target = ResolutionTarget(kind="record", id=event.memory_ids_touched[0])
            else:
                raw = bound.ingest([IngestEvent(kind="tool_result", role="tool", content=text)])[0]
                identity = uuid4()
                with transaction(service.engine) as conn:
                    derived_record(
                        conn,
                        scope,
                        Scope.model_validate(scope.model_dump()),
                        memory_id=identity,
                        version=1,
                        kind=kind,
                        content=text,
                        sources=[raw.event_id],
                        event_time=datetime.now(UTC),
                    )
                target = ResolutionTarget(kind="record", id=identity)
            fixtures.append((kind, detail, scope, bound, target))
        drain(service)
        for kind, detail, scope, bound, target in fixtures:
            item = bound.get_resolutions(target).sections[0]
            with service.engine.connect() as conn:
                usage = (
                    conn.execute(select(llm_calls).where(visible_filter(llm_calls, scope)))
                    .mappings()
                    .all()
                )
            output.append(
                {
                    "kind": kind,
                    "full_tokens": item.full.tokens,
                    "summary_tokens": item.summary.tokens,
                    "line_tokens": item.line.tokens,
                    "summary_method": item.summary.method,
                    "line_method": item.line.method,
                    "source_grounded": all(
                        line in item.full.text
                        for line in (*item.summary.text.splitlines(), item.line.text)
                    ),
                    "expected_in_summary": detail.rstrip(".") in item.summary.text,
                    "model_input_tokens": sum(row["input_tokens"] for row in usage),
                    "model_output_tokens": sum(row["output_tokens"] for row in usage),
                    "calls": len(usage),
                    "call_errors": [row["error_code"] for row in usage if row["error_code"]],
                }
            )
    _write(
        args.output / "metrics.json",
        {
            "model": settings.task_model("resolution_summarize").model,
            "prompt": VERSION,
            "elapsed_ms": round((perf_counter() - start) * 1000),
            "cases": output,
        },
    )


if __name__ == "__main__":
    main()
