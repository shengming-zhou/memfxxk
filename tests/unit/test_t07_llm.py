import json
from pathlib import Path

import httpx

from memfxxk.config import EvalModelSettings
from memfxxk.eval.llm import ChatClient


def test_t07_chat_cache_hits_without_network(tmp_path: Path) -> None:
    sent = []

    def fake(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "example"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 2},
            },
        )

    config = EvalModelSettings(base_url="https://example.invalid/v1", model="test")
    messages = [{"role": "user", "content": "hello"}]
    with ChatClient(config, tmp_path, httpx.MockTransport(fake)) as client:
        first = client.complete(messages)
    with ChatClient(config, tmp_path, httpx.MockTransport(fake)) as client:
        second = client.complete(messages)
        changed = client.complete(messages, max_tokens=1)
    assert second.cache_hit and not first.cache_hit and not changed.cache_hit
    assert (second.input_tokens, second.output_tokens) == (9, 2)
    assert len(sent) == 2
    assert all(body["temperature"] == 0 for body in sent)
