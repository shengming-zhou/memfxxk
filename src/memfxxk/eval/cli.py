import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, cast

import typer

from memfxxk.config import Settings
from memfxxk.db import create_engine
from memfxxk.embedding import CachedEmbedder, make_embedding_provider
from memfxxk.service import MemoryService

from .llm import ChatClient
from .longmemeval.dataset import DATA_DIRECTORY, DATA_FILES, Dataset, fetch_datasets
from .longmemeval.evidence import run_evidence
from .longmemeval.ingestion import ingest_dataset
from .longmemeval.judging import judge_results
from .longmemeval.metrics import markdown_metrics
from .longmemeval.qa import QAOptions, run_qa
from .longmemeval.qa_evidence import EVIDENCE_GROUPS, load_evidence_run
from .longmemeval.retrieval import (
    CHANNELS,
    RESULTS_DIRECTORY,
    Channel,
    RetrievalOptions,
    run_retrieval,
)
from .longmemeval.suite import load_suite

eval_cli = typer.Typer(help="Evaluate memory performance.")
longmemeval_cli = typer.Typer(help="Run the LongMemEval benchmark.")
eval_cli.add_typer(longmemeval_cli, name="longmemeval")


@contextmanager
def _service() -> Iterator[MemoryService]:
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        yield MemoryService(engine, settings)
    finally:
        engine.dispose()


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for part in value.split(",") if (item := part.strip())))


@longmemeval_cli.command("fetch")
def fetch_command(
    dataset: Annotated[Dataset | None, typer.Option(help="Fetch only this split.")] = None,
    data_dir: Path = DATA_DIRECTORY,
) -> None:
    """Download official datasets and record their SHA-256 hashes."""
    splits = (dataset,) if dataset is not None else tuple(DATA_FILES)
    typer.echo(fetch_datasets(data_dir, splits))


@longmemeval_cli.command("ingest")
def ingest_command(
    dataset: Annotated[Dataset, typer.Option()] = "s",
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    question_types: str = "",
    reset: bool = False,
    data_dir: Path = DATA_DIRECTORY,
    suite: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    """Ingest question histories and drain their embedding jobs."""
    try:
        data_file = data_dir / DATA_FILES[dataset]
        ids = load_suite(suite, dataset, data_file).question_ids if suite else ()
        with _service() as service:
            summary = ingest_dataset(
                service,
                data_file,
                dataset,
                limit=limit,
                question_types=_csv(question_types),
                question_ids=ids,
                reset=reset,
            )
    except (FileNotFoundError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(
        f"Ingested {summary.inserted}, skipped {summary.skipped}; "
        f"cache hit rate {summary.cache_hit_rate:.1%} "
        f"({summary.cache_hits} avoided provider inputs, {summary.cache_misses} sent)"
    )


@longmemeval_cli.command("retrieval")
def retrieval_command(
    dataset: Annotated[Dataset, typer.Option()] = "s",
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    question_types: str = "",
    channels: str = "lexical,vector,hybrid",
    k: str = "5,10,20",
    run_name: str = "longmemeval-retrieval",
    data_dir: Path = DATA_DIRECTORY,
    results_dir: Path = RESULTS_DIRECTORY,
) -> None:
    """Compare evidence recall using lexical, vector and hybrid retrieval."""
    channel_list = _csv(channels)
    if not channel_list or any(channel not in CHANNELS for channel in channel_list):
        raise typer.BadParameter("Choose lexical, vector or hybrid", param_hint="--channels")
    try:
        options = RetrievalOptions(
            dataset=dataset,
            limit=limit,
            question_types=_csv(question_types),
            channels=cast(tuple[Channel, ...], channel_list),
            k_list=tuple(int(value) for value in _csv(k)),
            run_name=run_name,
        )
        with _service() as service:
            path = run_retrieval(service, data_dir / DATA_FILES[dataset], options, results_dir)
    except (FileNotFoundError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(path)


@eval_cli.command("report")
def report_command(
    results_dirs: Annotated[list[Path], typer.Argument(exists=True, file_okay=False)],
) -> None:
    """Display retrieval and judged QA metrics."""
    runs = []
    qa_reports = []
    for run_dir in results_dirs:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("kind") == "qa":
            if not manifest.get("judge"):
                raise typer.BadParameter(f"Judge the QA run first: {run_dir}")
            report = (run_dir / "metrics.md").read_text(encoding="utf-8")
            qa_reports.append(f"## {run_dir.name}\n\n{report}")
            continue
        if manifest.get("kind") not in {"retrieval", "evidence"}:
            raise typer.BadParameter(f"Unrecognized evaluation run: {run_dir}")
        metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        runs.append((run_dir.name, metrics, tuple(manifest["k_list"])))
    if runs:
        typer.echo(markdown_metrics(runs), nl=False)
    for report in qa_reports:
        typer.echo(f"\n{report}", nl=False)


@longmemeval_cli.command("evidence")
def evidence_command(
    suite: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    dataset: Annotated[Dataset, typer.Option()] = "s",
    mem0: bool = False,
    channels: str = "lexical,vector,hybrid",
    run_name: str = "longmemeval-evidence",
    data_dir: Path = DATA_DIRECTORY,
    results_dir: Path = RESULTS_DIRECTORY,
) -> None:
    """Compare evidence on a frozen suite without generation or judging."""
    channel_list = _csv(channels)
    if not channel_list or any(channel not in CHANNELS for channel in channel_list):
        raise typer.BadParameter("Choose lexical, vector or hybrid", param_hint="--channels")
    data_file = data_dir / DATA_FILES[dataset]
    try:
        plan = load_suite(suite, dataset, data_file)
        with ExitStack() as stack:
            service = stack.enter_context(_service())
            external = {}
            if mem0:
                from .evidence_backends import Mem0EvidenceBackend

                scratch = stack.enter_context(TemporaryDirectory(prefix="memfxxk-evidence-"))
                embedding = make_embedding_provider(service.settings)
                external["mem0-raw"] = stack.enter_context(
                    Mem0EvidenceBackend(
                        embedding,
                        Path(scratch),
                        document_embedding=CachedEmbedder(service.engine, embedding),
                    )
                )
            path = run_evidence(
                service,
                data_file,
                plan,
                results_dir,
                run_name,
                external,
                channels=cast(tuple[Channel, ...], channel_list),
            )
    except (FileNotFoundError, ImportError, RuntimeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(path)


@longmemeval_cli.command("qa")
def qa_command(
    dataset: Annotated[Dataset, typer.Option()] = "s",
    groups: str = "G1,G3,G4",
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    k: Annotated[int, typer.Option(min=1, max=100)] = 8,
    evidence_budget: Annotated[int, typer.Option(min=0)] = 1500,
    channels: str = "hybrid",
    max_tool_calls: Annotated[int, typer.Option(min=0)] = 6,
    max_context_tokens: Annotated[int, typer.Option(min=1)] = 120_000,
    max_answer_tokens: Annotated[int | None, typer.Option(min=1)] = None,
    no_cache: bool = False,
    run_name: str = "longmemeval-qa",
    data_dir: Path = DATA_DIRECTORY,
    results_dir: Path = RESULTS_DIRECTORY,
    suite: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    evidence_run: Annotated[Path | None, typer.Option(exists=True, file_okay=False)] = None,
) -> None:
    """Generate answers for the selected LongMemEval QA groups."""
    if not run_name or Path(run_name).name != run_name or run_name in (".", ".."):
        raise typer.BadParameter("Choose a directory name", param_hint="--run-name")
    try:
        data_file = data_dir / DATA_FILES[dataset]
        plan = load_suite(suite, dataset, data_file) if suite else None
        if evidence_run is not None and plan is None:
            raise ValueError("Provide --suite with --evidence-run")
        groups_list = _csv(groups)
        evidence = (
            load_evidence_run(
                evidence_run,
                plan,
                tuple(group for group in groups_list if group in EVIDENCE_GROUPS),
            )
            if evidence_run is not None and plan is not None
            else None
        )
        with _service() as service:
            options = QAOptions(
                groups=groups_list,
                k=k,
                evidence_budget=evidence_budget,
                channels=cast(Channel, channels),
                max_tool_calls=max_tool_calls,
                max_context_tokens=max_context_tokens,
                concurrency=service.settings.eval.concurrency,
                max_answer_tokens=max_answer_tokens,
            )
            cache_dir = None if no_cache else Path("eval/cache/llm")
            with ChatClient(service.settings.eval.gen, cache_dir=cache_dir) as generator:
                path = run_qa(
                    service,
                    data_file,
                    dataset,
                    results_dir / run_name,
                    generator,
                    options,
                    limit=limit,
                    question_ids=plan.question_ids if plan is not None else (),
                    evidence_run=evidence,
                )
    except (FileNotFoundError, FileExistsError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(path)


@longmemeval_cli.command("judge")
def judge_command(
    results_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    official: bool = False,
    no_cache: bool = False,
) -> None:
    """Score QA hypotheses with a configured judge."""
    settings = Settings().eval
    try:
        if official:
            if settings.official_repo is None:
                raise ValueError("Set MEMFXXK_EVAL__OFFICIAL_REPO to the LongMemEval checkout")
            path = judge_results(
                results_dir,
                official_repo=Path(settings.official_repo),
                official_model=settings.judge.model or None,
            )
        else:
            cache_dir = None if no_cache else Path("eval/cache/llm")
            with ChatClient(settings.judge, cache_dir=cache_dir) as judge:
                path = judge_results(results_dir, judge)
    except (FileNotFoundError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(path)
