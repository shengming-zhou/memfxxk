"""Use at most six model calls to validate a neutral conflict-checking prompt."""

import argparse
import json
from pathlib import Path

import tiktoken
from run_m2_paired import _isolated_service, _write
from run_t17_writing import CHECK_SYSTEM, CHECK_VERSION, SUITE, evaluate, original, render

from memfxxk.config import Settings
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.priorities import PriorityTarget, SetPriority
from memfxxk.domain.story import LoreKeys
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatClient

CHECKS = ("c01", "c05", "c06")
ARMS = ("fixed", "adaptive")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(token_counter="tiktoken")
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    docs = suite["documents"]
    evidence = {doc["path"]: original(doc) for doc in docs}
    cases = {case["id"]: case for case in suite["cases"]}
    tokenizer = tiktoken.get_encoding("o200k_base")
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    # Disable retries so the pilot cannot send more than six completion requests.
    model = settings.eval.gen.model_copy(update={"max_retries": 0})
    with (
        _isolated_service(settings) as base,
        ChatClient(model, cache_dir=args.output / ".cache") as client,
    ):
        bound = base.for_request(EVAL_PRINCIPAL, eval_context("t17-check-pilot"))
        for doc in docs:
            bound.put_document(
                doc["path"],
                PutDocumentRequest(
                    content=evidence[doc["path"]],
                    load_policy=doc["policy"],
                    lore={doc["heading"]: LoreKeys(keywords=doc["keys"])},
                ),
            )
            bound.set_priority(
                SetPriority(
                    target=PriorityTarget(kind="document", path=doc["path"]),
                    priority=doc["priority"],
                )
            )
        drain(base)
        for case_id in CHECKS:
            case = cases[case_id]
            for arm in ARMS:
                context, _ = render(
                    arm, bound, docs, case["text"], suite["budget_tokens"], tokenizer
                )
                response = client.complete(
                    [
                        {
                            "role": "system",
                            "content": CHECK_SYSTEM + "\n\nSTORY EVIDENCE:\n" + context,
                        },
                        {
                            "role": "user",
                            "content": (
                                "Does this draft contradict any supplied rule?\n\n"
                                f"Draft:\n{case['text']}"
                            ),
                        },
                    ],
                    max_tokens=350,
                )
                rows.append(
                    {
                        "case": case_id,
                        "arm": arm,
                        "grade": evaluate(
                            response.message.get("content", ""), case, context, evidence
                        ),
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "latency_ms": round(response.latency_ms, 2),
                        "cache_hit": response.cache_hit,
                    }
                )
                _write(
                    args.output / "metrics.json",
                    {"model": model.model, "prompt": CHECK_VERSION, "cases": rows},
                )


if __name__ == "__main__":
    main()
