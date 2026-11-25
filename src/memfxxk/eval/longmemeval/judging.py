import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from memfxxk.eval.llm import ChatModel

from .dataset import Question, file_sha256, iter_questions
from .qa import _write_jsonl, read_prompt
from .retrieval import _write_json

JUDGE_PROMPT = "judge-v1.txt"
UPSTREAM_JUDGE_COMMIT = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
_EXACT = (
    "Accept an equivalent or complete answer, or reasoning that establishes it. "
    "Reject partial answers."
)
_RUBRICS = {
    "single-session-user": _EXACT,
    "single-session-assistant": _EXACT,
    "multi-session": _EXACT,
    "temporal-reasoning": _EXACT + " Tolerate an off-by-one error for duration counts.",
    "knowledge-update": (
        "Accept the required updated answer, even if the response also mentions earlier facts."
    ),
    "single-session-preference": (
        "Accept if the response correctly applies the user's personal information. "
        "The response can omit individual rubric points."
    ),
}
_ABSTENTION = (
    "Accept if the response recognizes the answer is unavailable, including when it gives other "
    "information without guessing the requested answer."
)
_OFFICIAL_MODELS = ("llama-3.1-70b-instruct", "gpt-4o-mini", "gpt-4o")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _grade(judge: ChatModel, question: Question, answer: str) -> dict[str, Any]:
    if question.abstention:
        reference_label, rubric = "Explanation", _ABSTENTION
    else:
        reference_label = (
            "Personalization rubric"
            if question.question_type.endswith("preference")
            else "Reference answer"
        )
        rubric = _RUBRICS[question.question_type]
    prompt = read_prompt(JUDGE_PROMPT).split("\n", 1)[1]
    reply = judge.complete(
        [
            {
                "role": "user",
                "content": prompt.format(
                    rubric=rubric,
                    question=question.question,
                    reference_label=reference_label,
                    reference=question.answer,
                    hypothesis=answer,
                ),
            }
        ],
        max_tokens=10,
    )
    text = reply.message.get("content")
    label = text.strip().casefold().rstrip(".") if isinstance(text, str) else ""
    if label not in {"yes", "no"}:
        raise ValueError(f"Judge did not reply Yes or No for {question.question_id}")
    return {
        "question_id": question.question_id,
        "correct": label == "yes",
        "llm_call": {key: value for key, value in asdict(reply).items() if key != "message"},
    }


def _official_grades(
    repo: Path, model: str, hypotheses: Path, dataset: Path
) -> list[dict[str, Any]]:
    script = (repo / "src/evaluation/evaluate_qa.py").resolve()
    if not script.is_file():
        raise ValueError("Clone the official LongMemEval repo and configure EVAL__OFFICIAL_REPO")
    if model not in _OFFICIAL_MODELS:
        raise ValueError(f"The official evaluator supports {', '.join(_OFFICIAL_MODELS)}")
    try:
        subprocess.run(
            [sys.executable, str(script), model, str(hypotheses.resolve()), str(dataset.resolve())],
            cwd=repo,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError(
            "The official evaluator failed. Install dependencies from its optional evaluator "
            "requirements and configure its model credentials or local endpoint."
        ) from error
    result = hypotheses.with_name(f"{hypotheses.name}.eval-results-{model}")
    return [
        {"question_id": row["question_id"], "correct": bool(row["autoeval_label"]["label"])}
        for row in _read_jsonl(result)
    ]


def _accuracy(rows: list[dict[str, Any]]) -> str:
    return f"{sum(row['correct'] for row in rows) / len(rows):.1%} ({len(rows)})" if rows else "—"


def _markdown(judge: str, results: dict[str, list[dict[str, Any]]]) -> str:
    rows = [
        "# LongMemEval QA",
        "",
        f"Judge: {judge}",
        "",
        "| Group | Accuracy | Correct abstention | Answer session found, answer wrong |",
        "| --- | --- | --- | --- |",
    ]
    by_type: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for group, entries in results.items():
        supported = [
            {"correct": not row["correct"]} for row in entries if row["answer_session_retrieved"]
        ]
        abstention = [row for row in entries if row["abstention"]]
        rows.append(
            f"| {group} | {_accuracy(entries)} | {_accuracy(abstention)} | {_accuracy(supported)} |"
        )
        typed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in entries:
            typed[row["question_type"]].append(row)
        by_type[group] = typed
    rows.extend(["", "| Group | Question type | Accuracy |", "| --- | --- | --- |"])
    rows.extend(
        f"| {group} | {name} | {_accuracy(grouped)} |"
        for group, types in by_type.items()
        for name, grouped in sorted(types.items())
    )
    return "\n".join(rows) + "\n"


def judge_results(
    results_dir: Path,
    judge: ChatModel | None = None,
    *,
    official_repo: Path | None = None,
    official_model: str | None = None,
) -> Path:
    if (judge is None) == (official_repo is None):
        raise ValueError("Choose a builtin judge or configure the official checkout")
    manifest_file = results_dir / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("kind") != "qa":
        raise ValueError("The results directory does not contain a QA run")
    dataset = Path(manifest["dataset_path"])
    if file_sha256(dataset) != manifest["sha256"]:
        raise ValueError("The dataset has changed since generation")
    observations = {
        (row["group"], row["question_id"]): row
        for row in _read_jsonl(results_dir / "per_question.jsonl")
    }
    groups = manifest["options"]["groups"]
    hypotheses = {group: _read_jsonl(results_dir / f"hypotheses.{group}.jsonl") for group in groups}
    results: dict[str, list[dict[str, Any]]] = {}
    if official_repo is not None:
        model = official_model or "gpt-4o-mini"
        label = f"official({model})"
        for group in groups:
            results[group] = (
                _official_grades(
                    official_repo, model, results_dir / f"hypotheses.{group}.jsonl", dataset
                )
                if hypotheses[group]
                else []
            )
    else:
        assert judge is not None
        label = f"builtin({judge.model})"
        needed = {row["question_id"] for group in groups for row in hypotheses[group]}
        questions = {}
        for question in iter_questions(dataset):
            if question.question_id in needed:
                questions[question.question_id] = question.model_copy(
                    update={
                        "haystack_sessions": [],
                        "haystack_dates": [],
                        "haystack_session_ids": [],
                    }
                )
                if len(questions) == len(needed):
                    break
        with ThreadPoolExecutor(max_workers=manifest["options"]["concurrency"]) as pool:
            for group in groups:
                results[group] = list(
                    pool.map(
                        lambda row: _grade(judge, questions[row["question_id"]], row["hypothesis"]),
                        hypotheses[group],
                    )
                )

    for group in groups:
        expected = {row["question_id"] for row in hypotheses[group]}
        received = {row["question_id"] for row in results[group]}
        if expected != received or len(results[group]) != len(expected):
            raise ValueError(f"The {group} judge returned incomplete or duplicate results")
    for group, entries in results.items():
        for row in entries:
            context = observations[group, row["question_id"]]
            row.update(
                judge=label,
                question_type=context["question_type"],
                abstention=context["abstention"],
                answer_session_retrieved=context["answer_session_retrieved"],
            )
        _write_jsonl(results_dir / f"judgements.{group}.jsonl", entries)
    manifest["judge"] = label
    if judge is not None:
        manifest["judge_prompt"] = JUDGE_PROMPT
        manifest["judge_source_commit"] = UPSTREAM_JUDGE_COMMIT
    else:
        manifest.pop("judge_prompt", None)
        manifest.pop("judge_source_commit", None)
    _write_json(manifest_file, manifest)
    (results_dir / "metrics.md").write_text(_markdown(label, results), encoding="utf-8")
    return results_dir
