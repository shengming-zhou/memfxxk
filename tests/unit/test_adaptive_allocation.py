from dataclasses import replace

from memfxxk.context.adaptive import Candidate, Variant, allocate
from memfxxk.domain.context import AdaptiveSettings, ContextReference


class CharacterCounter:
    def count(self, text: str) -> int:
        return len(text.replace("\n", ""))


def candidate(name, priority, relevance, *sizes, partial=0):
    levels = ("line", "summary", "full") if len(sizes) == 3 else ("line", "full")
    return Candidate(
        ContextReference(kind="document", ref_id=name, reason="test"),
        "documents",
        priority,
        relevance,
        tuple(Variant(level, "x" * size, size) for level, size in zip(levels, sizes)),
        (Variant("full", "p" * partial, partial),) if partial else (),
    )


def pack(budget, *items, **settings):
    config = AdaptiveSettings(flex_ratio=settings.pop("flex_ratio", 0), **settings)
    return allocate(list(items), budget, config, CharacterCounter())


def levels(result):
    return {index: item.level for index, item in result.ordered}


def test_waterfill_recycles_an_underfilled_high_layer():
    result = pack(100, candidate("A", 99, 1, 5, 10), candidate("B", -9, 1, 10, 70, 90))
    assert [(layer.allocated, layer.used) for layer in result.trace.layers] == [(10, 10), (90, 90)]


def test_floors_reduce_from_the_lowest_priority_and_keep_the_best_line():
    result = pack(
        15,
        candidate("A", 99, 0.2, 10, 30),
        candidate("B", 99, 1, 8, 30),
        candidate("C", -9, 1, 8, 30),
        floor_items=2,
    )
    assert [layer.floor_reduced for layer in result.trace.layers] == [1, 1]
    assert levels(result) == {1: "line"}


def test_upgrade_prefers_gain_per_token_after_putting_in_both_lines():
    result = pack(41, candidate("A", 5, 1, 8, 38, 90), candidate("B", 5, 0.8, 8, 18, 30))
    assert levels(result) == {0: "line", 1: "full"}


def test_repair_prefers_one_downgrade_over_flex():
    result = pack(
        108,
        candidate("A", 5, 1, 10, 80),
        candidate("B", 5, 0.4, 10, 30),
        flex_ratio=0.03,
    )
    assert levels(result) == {0: "full", 1: "line"}
    assert [(repair.step, repair.donor_ref_id) for repair in result.trace.repairs] == [
        ("downgrade", "B")
    ]


def test_near_fit_uses_flex_then_paragraph_provenance():
    full = pack(100, candidate("A", 5, 1, 10, 100), flex_ratio=0.03)
    assert levels(full) == {0: "full"}
    assert [repair.step for repair in full.trace.repairs] == ["flex"]
    partial = pack(95, candidate("A", 5, 1, 10, 100, partial=90))
    assert partial.ordered[0][1].tokens == 90
    assert partial.reasons[0] == "paragraph_with_source_reference"
    assert [repair.step for repair in partial.trace.repairs] == ["paragraph"]


def test_high_priority_cannot_displace_a_much_more_relevant_lower_layer():
    result = pack(
        85,
        candidate("A", 99, 0.02, 10, 47),
        candidate("B", 3, 1, 10, 40),
        decay=1,
        near_fit_ratio=0.20,
    )
    assert levels(result) == {0: "line", 1: "full"}
    assert not result.trace.repairs


def test_highest_priority_prefix_order_is_independent_of_scores_and_integer_size():
    first = candidate("A", 10**120, 0.1, 3, 5)
    second = candidate("B", 10**120, 1, 3, 5)
    other = candidate("C", -(10**120), 1, 3, 5)
    order = [i for i, _ in pack(30, first, second, other).ordered]
    changed = pack(30, replace(first, relevance=1), replace(second, relevance=0.01), other)
    assert order == [i for i, _ in changed.ordered] == [0, 1, 2]
