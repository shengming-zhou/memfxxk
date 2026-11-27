from importlib.resources import files
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

VERSION = "context-weights-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coefficients: dict[int, float] = Field(max_length=100)

    @field_validator("coefficients")
    @classmethod
    def finite(cls, value: dict[int, float]) -> dict[int, float]:
        if not all(isfinite(number) for number in value.values()):
            raise ValueError("Coefficients must be finite")
        return value


SCHEMA = TypeAdapter(Plan)
