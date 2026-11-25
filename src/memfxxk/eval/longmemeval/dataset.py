import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import file_digest, sha256
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, JsonValue, model_validator

from memfxxk.ids import derived_id

Dataset = Literal["s", "m", "oracle"]
DATA_DIRECTORY = Path("eval/data/longmemeval")
DATA_FILES: dict[Dataset, str] = {
    "s": "longmemeval_s_cleaned.json",
    "m": "longmemeval_m_cleaned.json",
    "oracle": "longmemeval_oracle.json",
}
# The official LongMemEval README links the cleaned release.
_RELEASE = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/"
_CHUNK_SIZE = 1024 * 1024


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    has_answer: bool = False


class Question(BaseModel):
    question_id: str
    question_type: str
    question: str
    answer: JsonValue
    question_date: str
    haystack_session_ids: list[str]
    haystack_dates: list[str]
    haystack_sessions: list[list[Message]]
    answer_session_ids: list[str]

    @model_validator(mode="after")
    def validate_sessions(self) -> "Question":
        if not (
            len(self.haystack_session_ids)
            == len(self.haystack_dates)
            == len(self.haystack_sessions)
        ):
            raise ValueError("Haystack session IDs, dates and histories must have the same length")
        if len(set(self.haystack_session_ids)) != len(self.haystack_session_ids):
            raise ValueError("Haystack session IDs must be unique within a question")
        return self

    @property
    def abstention(self) -> bool:
        return self.question_id.endswith("_abs")

    @property
    def asked_at(self) -> datetime:
        return parse_date(self.question_date)


@dataclass(frozen=True, slots=True)
class Turn:
    session_id: str
    official_session_id: str
    turn_index: int
    event_time: datetime
    messages: tuple[Message, ...]

    @property
    def turn_id(self) -> str:
        return f"{self.session_id}:{self.turn_index}"

    @property
    def memory_id(self) -> UUID:
        return derived_id("turn", "eval", self.session_id, self.turn_id)

    @property
    def has_answer(self) -> bool:
        return any(message.has_answer for message in self.messages)


def parse_date(value: str) -> datetime:
    # The release records wall times without a zone. All comparisons treat them as UTC.
    without_weekday = re.sub(r"\s+\([A-Za-z]{3}\)(?=\s)", "", value)
    return datetime.strptime(without_weekday, "%Y/%m/%d %H:%M").replace(tzinfo=UTC)


def pair_turns(messages: list[Message]) -> Iterator[tuple[Message, ...]]:
    index = 0
    while index < len(messages):
        stop = index + 1
        if (
            messages[index].role == "user"
            and stop < len(messages)
            and messages[stop].role == "assistant"
        ):
            stop += 1
        yield tuple(messages[index:stop])
        index = stop


def iter_turns(question: Question) -> Iterator[Turn]:
    for session, date, messages in zip(
        question.haystack_session_ids,
        question.haystack_dates,
        question.haystack_sessions,
        strict=True,
    ):
        session_id = f"{question.question_id}:{session}"
        event_time = parse_date(date)
        for index, turn in enumerate(pair_turns(messages)):
            yield Turn(session_id, session, index, event_time, turn)


def iter_questions(path: Path) -> Iterator[Question]:
    """Decode one object at a time, including the multi-gigabyte M split."""
    decoder = json.JSONDecoder()
    with path.open(encoding="utf-8-sig") as source:
        buffer = ""

        def next_token() -> str:
            nonlocal buffer
            buffer = buffer.lstrip()
            while not buffer:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    return ""
                buffer = chunk.lstrip()
            return buffer[0]

        if next_token() != "[":
            raise ValueError(f"Expected a JSON array in {path}")
        buffer = buffer[1:]
        if next_token() != "]":
            while True:
                if next_token() != "{":
                    raise ValueError(f"Expected a question object in {path}")
                while True:
                    try:
                        item, end = decoder.raw_decode(buffer)
                    except json.JSONDecodeError:
                        chunk = source.read(_CHUNK_SIZE)
                        if not chunk:
                            raise
                        buffer += chunk
                    else:
                        buffer = buffer[end:]
                        break
                yield Question.model_validate(item)
                token = next_token()
                if token == "]":
                    break
                if token != ",":
                    raise ValueError(f"Expected ',' or ']' after question in {path}")
                buffer = buffer[1:]
        buffer = buffer[1:]
        if next_token():
            raise ValueError(f"Unexpected contents after dataset array in {path}")


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def fetch_datasets(directory: Path, datasets: tuple[Dataset, ...]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    entries = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for dataset in datasets:
            filename = DATA_FILES[dataset]
            path = directory / filename
            url = _RELEASE + filename
            digest = _download(client, url, path) if not path.exists() else file_sha256(path)
            entries[dataset] = {"file": filename, "url": url, "sha256": digest}
            manifest_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def _download(client: httpx.Client, url: str, path: Path) -> str:
    partial = path.with_suffix(f".{uuid4().hex}.part")
    digest = sha256()
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with partial.open("xb") as target:
                for chunk in response.iter_bytes(_CHUNK_SIZE):
                    target.write(chunk)
                    digest.update(chunk)
        partial.replace(path)
        return digest.hexdigest()
    finally:
        partial.unlink(missing_ok=True)
