from collections.abc import Callable
from dataclasses import dataclass, field
from math import fsum
from typing import Literal

from memfxxk.context.priorities import priority_layers
from memfxxk.domain.context import (
    AdaptiveSettings,
    AllocationRepair,
    AllocationTrace,
    ContextReference,
    LayerAllocation,
    SectionName,
)
from memfxxk.domain.resolutions import ResolutionLevel
from memfxxk.text import TokenCounter

LEVELS: tuple[ResolutionLevel, ...] = ("line", "summary", "full")


@dataclass(frozen=True, slots=True)
class Variant:
    level: ResolutionLevel
    text: str
    tokens: int


@dataclass(frozen=True, slots=True)
class Candidate:
    ref: ContextReference
    section: SectionName
    priority: int
    relevance: float
    variants: tuple[Variant, ...]
    partials: tuple[Variant, ...] = ()
    excluded: str | None = None
    steady: bool = False

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (
            self.ref.kind,
            self.ref.ref_id,
            self.ref.section_index if self.ref.section_index is not None else -1,
            str(self.ref.graph_edge_id or ""),
        )


@dataclass(slots=True)
class Layer:
    priority: int
    weight: float
    items: list[int]
    floor: list[int]
    demand: int
    allocated: int = 0
    reduced: int = 0
    used: int = 0


def waterfill(capacity: int, layers: list[Layer]) -> None:
    remaining = capacity - sum(layer.allocated for layer in layers)
    active = [layer for layer in layers if layer.allocated < layer.demand]
    while remaining > 0 and active:
        weight_sum = fsum(layer.weight for layer in active)
        shares = [remaining * (layer.weight / weight_sum) for layer in active]
        grants = [
            min(layer.demand - layer.allocated, int(share))
            for layer, share in zip(active, shares, strict=True)
        ]
        if not any(grants):
            grants[max(range(len(active)), key=shares.__getitem__)] = 1
        for layer, grant in zip(active, grants, strict=True):
            layer.allocated += grant
            remaining -= grant
        active = [layer for layer in active if layer.allocated < layer.demand]


@dataclass(slots=True)
class Result:
    ordered: list[tuple[int, Variant]]
    reasons: dict[int, str]
    trace: AllocationTrace


@dataclass(slots=True)
class Packing:
    candidates: list[Candidate]
    settings: AdaptiveSettings
    layers: list[Layer]
    counter: TokenCounter
    budget: int
    flex: int
    by_item: dict[int, Layer] = field(init=False)
    selected: dict[int, Variant] = field(default_factory=dict)
    protected: set[int] = field(default_factory=set)
    repaired: set[int] = field(default_factory=set)
    reasons: dict[int, str] = field(default_factory=dict)
    repairs: list[AllocationRepair] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.by_item = {index: layer for layer in self.layers for index in layer.items}

    def put(self, index: int, variant: Variant | None) -> None:
        old = self.selected.pop(index, None)
        self.by_item[index].used += (variant.tokens if variant else 0) - (old.tokens if old else 0)
        if variant is not None:
            self.selected[index] = variant

    def render_order(self, index: int) -> tuple:
        item = self.candidates[index]
        # The same high-priority sources keep their order even when scores change.
        return (
            -item.priority,
            not item.steady,
            0 if item.priority == self.layers[0].priority else -item.relevance,
            item.key,
        )

    def utility(self, index: int) -> float:
        item = self.candidates[index]
        rank = self.layers.index(self.by_item[index])
        return item.relevance + self.settings.relevance_gap * 0.9 / (rank + 1)

    def upgrades(self) -> list[tuple[float, int, Variant]]:
        choices = []
        for index, old in self.selected.items():
            if index in self.repaired:
                continue
            variants = self.candidates[index].variants
            position = variants.index(old) + 1
            if position < len(variants):
                variant = variants[position]
                delta = variant.tokens - old.tokens
                gain = self.utility(index) * (0.55 if variant.level == "full" else 1.0)
                choices.append((gain / max(1, delta), index, variant))
        return sorted(choices, key=lambda x: (-x[0], self.render_order(x[1])))

    def free(self, index: int) -> int:
        layer = self.by_item[index]
        return layer.allocated - layer.used

    def record(
        self,
        index: int,
        step: Literal["downgrade", "flex", "paragraph", "exact_budget"],
        donor: int | None = None,
    ) -> None:
        item = self.candidates[index]
        self.repairs.append(
            AllocationRepair(
                priority=item.priority,
                ref_id=item.ref.ref_id,
                step=step,
                donor_ref_id=self.candidates[donor].ref.ref_id if donor is not None else None,
            )
        )

    def trade(self, index: int, required: int) -> bool:
        current = self.candidates[index]
        donors = []
        for donor, previous in self.selected.items():
            candidate = self.candidates[donor]
            if (
                donor == index
                or donor in self.repaired
                or candidate.priority > current.priority
                or candidate.relevance > current.relevance + self.settings.relevance_gap
            ):
                continue
            position = candidate.variants.index(previous)
            if position:
                smaller = candidate.variants[position - 1]
                penalty = self.utility(donor) * (0.55 if previous.level == "full" else 1.0)
                donors.append((penalty, donor, smaller, previous.tokens - smaller.tokens))
        if not donors:
            return False
        penalty, donor, smaller, freed = min(
            donors, key=lambda entry: (entry[0], self.render_order(entry[1]))
        )
        if freed < required or penalty >= self.utility(index):
            return False
        origin, target = self.by_item[donor], self.by_item[index]
        if origin is not target:
            target.allocated += required
            origin.allocated -= required
        self.put(donor, smaller)
        self.repaired.add(donor)
        self.reasons[donor] = "downgraded_for_near_fit"
        self.record(index, "downgrade", donor)
        return True

    def repair(self) -> None:
        for _, index, next_variant in self.upgrades():
            previous = self.selected[index]
            if index in self.repaired or next_variant.tokens <= previous.tokens:
                continue
            available = max(0, self.free(index))
            missing = next_variant.tokens - previous.tokens - available
            if missing <= 0:
                self.put(index, next_variant)
                continue
            if missing > next_variant.tokens * self.settings.near_fit_ratio:
                continue
            if self.trade(index, missing):
                self.put(index, next_variant)
            elif missing <= self.flex:
                self.by_item[index].allocated += missing
                self.flex -= missing
                self.put(index, next_variant)
                self.record(index, "flex")
            else:
                partial = next(
                    (
                        option
                        for option in self.candidates[index].partials
                        if previous.tokens < option.tokens <= previous.tokens + available
                    ),
                    None,
                )
                if partial is None:
                    continue
                self.put(index, partial)
                self.reasons[index] = "paragraph_with_source_reference"
                self.record(index, "paragraph")
            self.repaired.add(index)

    def exact(self) -> list[tuple[int, Variant]]:
        ordered = sorted(self.selected.items(), key=lambda entry: self.render_order(entry[0]))
        while self.counter.count("\n\n".join(value.text for _, value in ordered)) > self.budget:
            index, previous = min(
                ordered,
                key=lambda entry: (
                    entry[0] in self.protected,
                    self.utility(entry[0]),
                    self.render_order(entry[0]),
                ),
            )
            smaller = [
                variant
                for variant in self.candidates[index].variants
                if variant.tokens < previous.tokens
            ]
            replacement = smaller[-1] if smaller else None
            self.put(index, replacement)
            self.reasons[index] = "exact_token_budget"
            self.record(index, "exact_budget")
            ordered = sorted(self.selected.items(), key=lambda entry: self.render_order(entry[0]))
        return ordered


def allocate(
    candidates: list[Candidate],
    budget: int,
    settings: AdaptiveSettings,
    counter: TokenCounter,
    *,
    multipliers: dict[int, float] | None = None,
    plan: Callable[[list[Layer]], dict[int, float]] | None = None,
) -> Result:
    order = sorted(
        (index for index, item in enumerate(candidates) if not item.excluded and item.variants),
        key=lambda index: (-candidates[index].relevance, candidates[index].key),
    )
    layers = []
    for rank, (priority, items) in enumerate(
        priority_layers(order, lambda index: candidates[index].priority)
    ):
        line_costs = [candidates[index].variants[0].tokens for index in items]
        demand = sum(line_costs) + sum(
            candidates[index].variants[-1].tokens - line_costs[position]
            for position, index in enumerate(items[: settings.expand_items])
        )
        floor = items[: settings.floor_items]
        weight = max(1e-10, settings.decay**rank) * (multipliers or {}).get(priority, 1.0)
        layers.append(Layer(priority, weight, items, floor, demand, sum(line_costs[: len(floor)])))
    original_weights = {layer.priority: layer.weight for layer in layers}
    coefficients = plan(layers) if plan is not None and layers and budget else {}
    for layer in layers:
        layer.weight *= coefficients.get(layer.priority, 1.0)

    floor_total = sum(layer.allocated for layer in layers)
    for layer in reversed(layers):
        while floor_total > budget and layer.floor:
            size = candidates[layer.floor.pop()].variants[0].tokens
            layer.allocated -= size
            floor_total -= size
            layer.reduced += 1
    floor_tokens = {layer.priority: layer.allocated for layer in layers}
    flex = min(int(budget * settings.flex_ratio), max(0, budget - floor_total))
    waterfill(budget - flex, layers)
    packing = Packing(candidates, settings, layers, counter, budget, flex)
    for layer in layers:
        for index in layer.floor:
            packing.put(index, candidates[index].variants[0])
            packing.protected.add(index)
        for index in layer.items[len(layer.floor) :]:
            line = candidates[index].variants[0]
            if line.tokens <= packing.free(index):
                packing.put(index, line)
    while True:
        choice = next(
            (
                (index, variant)
                for _, index, variant in packing.upgrades()
                if variant.tokens - packing.selected[index].tokens <= packing.free(index)
            ),
            None,
        )
        if choice is None:
            break
        packing.put(*choice)
    packing.repair()
    ordered = packing.exact()
    return Result(
        ordered,
        packing.reasons,
        AllocationTrace(
            budget=budget,
            flex_tokens=flex,
            plan_input_weights=original_weights if coefficients else {},
            plan_coefficients=coefficients,
            layers=[
                LayerAllocation(
                    priority=layer.priority,
                    weight=layer.weight,
                    demand=layer.demand,
                    allocated=layer.allocated,
                    used=layer.used,
                    floor_tokens=floor_tokens[layer.priority],
                    floor_reduced=layer.reduced,
                )
                for layer in layers
            ],
            repairs=packing.repairs,
        ),
    )
