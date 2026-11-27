import pytest

from memfxxk.config import Settings
from memfxxk.llm.settings import LLMSettings, TaskModel

MEMORY_MODELS = LLMSettings(default=TaskModel(model="fake"))


def test_explicit_options_override_defaults_from_environment(monkeypatch):
    monkeypatch.setenv("MEMFXXK_PRESET", "full")
    monkeypatch.setenv("MEMFXXK_STRATEGIES__NOTES", "off")
    monkeypatch.setenv("MEMFXXK_SEARCH__RERANKER", "off")
    monkeypatch.setenv("MEMFXXK_SLEEPTIME__EVERY_TURNS", "8")
    settings = Settings(llm=MEMORY_MODELS, _env_file=None)
    assert settings.strategies.notes == "off" and settings.search.reranker == "off"
    assert settings.strategies.tiers == "memoryos" and settings.strategies.lessons == "both"
    assert settings.strategies.auto_graph_hops == 1 and settings.fact_strategy == "mem0_additive"
    assert not settings.strategies.amem_enrich_mem0_facts
    assert settings.sleeptime.every_turns == 8 and settings.sleeptime.after_compaction


def test_baseline_and_named_strategy_starting_points():
    assert Settings(_env_file=None).strategies.graph == "links"
    baseline = Settings(preset="baseline", _env_file=None)
    assert baseline.strategies.graph == "off" and baseline.fact_strategy == "off"
    mem0 = Settings(preset="mem0", llm=MEMORY_MODELS, _env_file=None)
    assert mem0.fact_strategy == "mem0_additive" and mem0.strategies.mem0_additive_ranking
    graph = Settings(preset="graphiti", llm=MEMORY_MODELS, _env_file=None)
    assert graph.strategies.graph == "graphiti" and graph.strategies.auto_graph_hops == 1
    letta = Settings(preset="letta", llm=MEMORY_MODELS, _env_file=None)
    assert letta.strategies.blocks == "letta" and letta.sleeptime.after_compaction
    with pytest.raises(ValueError, match="Fact extraction requires"):
        Settings(preset="full", llm=LLMSettings(), _env_file=None)
