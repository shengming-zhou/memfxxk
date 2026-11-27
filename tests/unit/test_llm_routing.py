from typing import get_args

from memfxxk.config import Settings
from memfxxk.llm.settings import Task


def test_task_settings_inherit_default_and_accept_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("MEMFXXK_LLM__DEFAULT__MODEL", "default-model")
    monkeypatch.setenv("MEMFXXK_LLM__DEFAULT__BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("MEMFXXK_LLM__TASKS__COMPACT__MODEL", "summary-model")
    monkeypatch.setenv("MEMFXXK_LLM__TASKS__COMPACT__TEMPERATURE", "0.3")
    monkeypatch.setenv("MEMFXXK_M2__EXTRACT_FACTS", "true")
    settings = Settings(_env_file=None)
    for task in get_args(Task):
        model = settings.task_model(task)
        assert model.base_url == "http://example.invalid/v1"
        assert model.model == ("summary-model" if task == "compact" else "default-model")
    assert settings.task_model("compact").temperature == 0.3
