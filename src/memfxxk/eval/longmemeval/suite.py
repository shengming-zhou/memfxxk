from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from .dataset import Dataset, file_sha256


class QuestionSuite(BaseModel):
    dataset: Dataset
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    question_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("question_ids")
    @classmethod
    def unique_ids(cls, ids: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("Suite question IDs must be nonempty and unique")
        return ids


def load_suite(path: Path, dataset: Dataset, data_file: Path) -> QuestionSuite:
    suite = QuestionSuite.model_validate_json(path.read_text(encoding="utf-8"))
    if suite.dataset != dataset or suite.dataset_sha256 != file_sha256(data_file):
        raise ValueError("Suite requires its pinned dataset and SHA-256")
    return suite
