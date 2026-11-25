import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from functools import cache
from math import ceil
from types import ModuleType
from typing import Literal, Protocol

import tiktoken

_CJK = (
    "\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u31f0-\u31ff"
    "\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
    "\U00020000-\U000323af"
)
_CJK_RUN = re.compile(f"[{_CJK}]+")
_WORD = re.compile(r"\w+(?:[./\\-]+\w+)*")
_PATH_SEPARATOR = re.compile(r"[/\\]+")
_SYMBOL_SEPARATOR = re.compile(r"[_.-]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_MAX_LEXEME_BYTES = 2046
_MAX_POSITION = 16383
_MAX_POSITIONS_PER_LEXEME = 256


class Tokenizer(Protocol):
    def tokenize(self, text: str) -> list[str]: ...


@cache
def _jieba() -> ModuleType:
    import jieba

    jieba.setLogLevel(logging.CRITICAL + 1)
    jieba.initialize()
    return jieba


def _identifier_terms(word: str) -> list[str]:
    terms = [word.lower()]
    for component in _PATH_SEPARATOR.split(word):
        terms.append(component.lower())
        for piece in _SYMBOL_SEPARATOR.split(component):
            terms.extend(part.lower() for part in _CAMEL_BOUNDARY.split(piece))
    return list(dict.fromkeys(term for term in terms if any(c.isalnum() for c in term)))


class MixedTokenizer:
    def __init__(self, stop_words: Iterable[str] = ()) -> None:
        self.stop_words = {unicodedata.normalize("NFKC", word).lower() for word in stop_words}

    def tokenize(self, text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", text)
        tokens: list[str] = []
        start = 0
        for match in _CJK_RUN.finditer(normalized):
            tokens.extend(self._latin_terms(normalized[start : match.start()]))
            tokens.extend(
                token
                for token in _jieba().cut_for_search(match.group().lower())
                if any(character.isalnum() for character in token)
            )
            start = match.end()
        tokens.extend(self._latin_terms(normalized[start:]))
        return [token for token in tokens if token not in self.stop_words]

    @staticmethod
    def _latin_terms(text: str) -> Iterable[str]:
        for match in _WORD.finditer(text):
            yield from _identifier_terms(match.group())


def _lexeme(token: str) -> str:
    return token.encode("utf-8")[:_MAX_LEXEME_BYTES].decode("utf-8", errors="ignore")


def _quote(token: str) -> str:
    return "'" + token.replace("\\", "\\\\").replace("'", "''") + "'"


def build_tsvector_literal(tokens: Iterable[str]) -> str:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, token in enumerate(tokens, start=1):
        lexeme = _lexeme(token)
        if not lexeme:
            continue
        position = min(index, _MAX_POSITION)
        existing = positions[lexeme]
        if len(existing) < _MAX_POSITIONS_PER_LEXEME and (not existing or existing[-1] != position):
            existing.append(position)
    return " ".join(
        f"{_quote(lexeme)}:{','.join(map(str, indices))}" for lexeme, indices in positions.items()
    )


def build_tsquery_literal(tokens: Iterable[str], op: str = "|") -> str | None:
    if op not in {"|", "&", "<->"}:
        raise ValueError("Unsupported tsquery operator")
    terms = [_quote(_lexeme(token)) for token in tokens if token]
    return f" {op} ".join(terms) or None


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


@cache
def _token_encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("o200k_base")


class TiktokenCounter:
    def count(self, text: str) -> int:
        return len(_token_encoding().encode_ordinary(text))


class SimpleCounter:
    def count(self, text: str) -> int:
        cjk_count = sum(len(match.group()) for match in _CJK_RUN.finditer(text))
        return cjk_count + ceil(len(_CJK_RUN.sub(" ", text).split()) * 1.3)


def make_token_counter(name: Literal["tiktoken", "simple"]) -> TokenCounter:
    return SimpleCounter() if name == "simple" else TiktokenCounter()
