from memfxxk.context.feedback import replay, step
from memfxxk.domain.context import AdaptiveSettings
from memfxxk.domain.feedback import AllocationState, FeedbackReference, LearningSignal


def test_bounded_updates_converge_and_replay_is_ordered():
    settings = AdaptiveSettings()
    initial = AllocationState(decay=settings.decay, expand_items=settings.expand_items)
    state = initial
    miss = LearningSignal(
        signal="retrieved", priority=19, target=FeedbackReference(kind="memory", ref_id="example")
    )
    previous = 1.0
    for _ in range(200):
        state = step(state, [miss], settings)
        current = state.multipliers[19]
        assert previous <= current <= min(1.5, previous + 0.03500001)
        previous = current
    assert 1.49 < state.multipliers[19] <= 1.5
    unused = miss.model_copy(update={"signal": "unused"})
    for _ in range(200):
        state = step(state, [unused], settings)
    assert 0.69 < state.multipliers[19] < 0.71
    assert replay(initial, []) == initial
