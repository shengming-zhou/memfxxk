from typing import Literal

StrategyPreset = Literal["baseline", "mem0", "graphiti", "letta", "full"]

PRESETS: dict[StrategyPreset, dict[str, dict[str, object]]] = {
    "baseline": {"strategies": {"facts": "off", "graph": "off"}},
    "mem0": {
        "strategies": {"facts": "mem0_additive", "mem0_additive_ranking": True, "graph": "off"},
    },
    "graphiti": {"strategies": {"facts": "off", "graph": "graphiti", "auto_graph_hops": 1}},
    "letta": {
        "strategies": {"facts": "off", "graph": "off", "blocks": "letta"},
        "sleeptime": {"every_turns": 16, "after_compaction": True},
    },
    "full": {
        "strategies": {
            "facts": "mem0_additive",
            "mem0_additive_ranking": True,
            "graph": "graphiti",
            "auto_graph_hops": 1,
            "notes": "amem",
            "amem_enrich_mem0_facts": False,
            "blocks": "letta",
            "tiers": "memoryos",
            "recall": "hindsight",
            "reflect": "hindsight",
            "lessons": "both",
        },
        "sleeptime": {"every_turns": 16, "after_compaction": True},
        "search": {"reranker": "llm"},
    },
}
