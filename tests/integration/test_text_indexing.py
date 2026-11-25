import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from memfxxk.text import MixedTokenizer, build_tsquery_literal, build_tsvector_literal

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("中文句子", ["中文", "句子"]),
        (
            "把 parseHTTPResponse 改成 async 版本",
            ["parsehttpresponse", "parse", "http", "response", "async", "版本"],
        ),
        ("path/to/file.py", ["path/to/file.py", "file.py", "file", "py"]),
        ("python 3.12", ["python", "3.12"]),
        ("snake_case_name kebab-case a.b.c", ["snake_case_name", "case", "kebab", "a.b.c"]),
    ],
)
def test_postgres_search_matches_tokenized_text(
    engine: Engine, source: str, expected: list[str]
) -> None:
    tokens = MixedTokenizer().tokenize(source)
    assert set(expected).issubset(tokens)

    with engine.connect() as connection:
        matches = connection.scalar(
            text("SELECT CAST(:document AS tsvector) @@ CAST(:query AS tsquery)"),
            {
                "document": build_tsvector_literal(tokens),
                "query": build_tsquery_literal(expected, op="&"),
            },
        )
    assert matches is True


def test_postgres_literals_keep_quoted_lexemes(engine: Engine) -> None:
    tokens = ["don't", "file\\name", "don't"]

    with engine.connect() as connection:
        matches = connection.scalar(
            text("SELECT CAST(:document AS tsvector) @@ CAST(:query AS tsquery)"),
            {
                "document": build_tsvector_literal(tokens),
                "query": build_tsquery_literal(tokens, op="&"),
            },
        )
    assert matches is True
