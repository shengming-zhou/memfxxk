from collections import Counter
from collections.abc import Mapping
from math import log1p
from uuid import UUID

from sqlalchemy.engine import RowMapping

from memfxxk.domain.retrieval import ScoreDetails
from memfxxk.text import MixedTokenizer


def apply_additive_weights(
    scores: Mapping[UUID, ScoreDetails],
    rows: Mapping[UUID, RowMapping],
    query_tokens: list[str],
) -> None:
    if not query_tokens:
        return
    tokenizer = MixedTokenizer()
    facts = {
        key: Counter(tokenizer.tokenize(rows[key]["content"]))
        for key in scores
        if rows[key]["type"] == "fact" and rows[key]["metadata"].get("strategy") == "mem0_additive"
    }
    if not facts:
        return
    terms = set(query_tokens)
    lengths = {key: counts.total() for key, counts in facts.items()}
    average = sum(lengths.values()) / len(facts) or 1
    document_frequencies = Counter(
        term for counts in facts.values() for term in counts if term in terms
    )
    inverse_frequency = {
        term: log1p((len(facts) - frequency + 0.5) / (frequency + 0.5))
        for term, frequency in document_frequencies.items()
    }
    distinct_times = sorted({rows[key]["valid_from"] or rows[key]["event_time"] for key in facts})
    age_order = {
        moment: i / max(1, len(distinct_times) - 1) for i, moment in enumerate(distinct_times)
    }
    k1, b = 1.2, 0.75
    for key, counts in facts.items():
        denominator = k1 * (1 - b + b * lengths[key] / average)
        bm25 = sum(
            weight * counts[term] * (k1 + 1) / (counts[term] + denominator)
            for term, weight in inverse_frequency.items()
            if term in counts
        )
        entities = rows[key]["metadata"].get("entities", [])
        matched_entities = sum(
            bool(entity_tokens := set(tokenizer.tokenize(entity))) and entity_tokens <= terms
            for entity in entities
        )
        entity_boost = min(0.5, matched_entities * 0.15)
        effective = rows[key]["valid_from"] or rows[key]["event_time"]
        multiplier = 1 + 0.3 * bm25 / (1 + bm25) + entity_boost + 0.05 * age_order[effective]
        scores[key].fact_bm25 = bm25
        scores[key].fact_entity_boost = entity_boost
        scores[key].boost *= multiplier
        scores[key].final *= multiplier
