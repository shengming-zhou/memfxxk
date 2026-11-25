# M1 evaluation smoke runs

These aggregate reports were produced with commit `d6a86b7`, PostgreSQL 17 and pgvector.

- [Oracle retrieval](oracle/metrics.md) uses the official LongMemEval Oracle split, 5,484 turn records, and a deterministic fake embedding provider with 128 dimensions. The 470 answerable questions contribute to recall and the other 30 are counted as abstentions. Hybrid search p95 was 18.84 ms, excluding query embedding.
- [QA smoke run](qa/metrics.md) uses the two-question test fixture, including one abstention. G1–G4 each completed two questions. The fake judge returned positive grades by design. [Cost counters](qa/costs.json) record 0, 28.5, 38.5, and 0 mean injected tokens and 0, 0, 0, and 3 mean tool calls in G1–G4 order.

The M1 baseline still requires the S split at full scale, real embedding and QA models, and a week of coding-agent usage through MCP.

Run the QA smoke test with `uv run pytest tests/integration/test_t07_qa.py`. Follow the [developer manual](../../../docs/developer-manual.md#run-evaluation) for Oracle retrieval commands and use the [retrieval manifest](oracle/manifest.json) to reproduce its embedding dimensions and model label.
