from memfxxk.context.priorities import priority_layers


def test_priority_layers_preserve_ties_and_compare_full_integers():
    items = [("a", 4), ("b", 10**80), ("c", -(10**80)), ("d", 4), ("e", 0)]
    assert priority_layers(items, lambda item: item[1]) == [
        (10**80, [items[1]]),
        (4, [items[0], items[3]]),
        (0, [items[4]]),
        (-(10**80), [items[2]]),
    ]
