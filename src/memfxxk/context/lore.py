import re
import unicodedata

from memfxxk.domain.story import LoreKeys

_NAME = re.compile(r"\b(?:[A-Z][a-z]{2,}(?:[-'][A-Z]?[a-z]+)?\s+){1,3}[A-Z][a-z]{2,}\b")
_ALIAS = re.compile(
    r'(?i:\baka\b|\balso known as\b|\bcalled\b|别名|又称)\s*(?i:the\s+)?["“「]?'
    r"([A-Z][A-Za-z0-9’ -]{1,64}|[\u3400-\u9fff]{2,16})"
)
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def automatic(heading: str, text: str) -> list[str]:
    name = heading.rsplit(" / ", 1)[-1].strip()
    excerpt = text[:16_000]
    return list(
        dict.fromkeys(filter(None, [name, *_ALIAS.findall(excerpt), *_NAME.findall(excerpt)]))
    )[:24]


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def triggered(query: str, phrases: list[str]) -> bool:
    haystack = _normalize(query)
    if not haystack:
        return False
    for phrase in phrases:
        needle = _normalize(phrase)
        if needle and (
            needle in haystack
            if _CJK.search(needle)
            else re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None
        ):
            return True
    return False


def matches(query: str, heading: str, text: str, explicit: dict | None = None) -> bool:
    terms = LoreKeys.model_validate(explicit or {})
    return triggered(query, [*terms.keywords, *terms.aliases, *automatic(heading, text)])
