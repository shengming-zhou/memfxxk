# M1 evaluation smoke runs

These aggregate reports use PostgreSQL 17 and pgvector. Each run records its code revision and configuration in a manifest.

- [Oracle retrieval](oracle/metrics.md) uses the official LongMemEval Oracle split, 5,484 turn records, and a deterministic fake embedding provider with 128 dimensions. The 470 answerable questions contribute to recall and the other 30 are counted as abstentions. Hybrid search p95 was 18.84 ms, excluding query embedding.
- [QA smoke run](qa/metrics.md) uses the two-question test fixture, including one abstention. G1–G4 each completed two questions. The fake judge returned positive grades by design. [Cost counters](qa/costs.json) record 0, 28.5, 38.5, and 0 mean injected tokens and 0, 0, 0, and 3 mean tool calls in G1–G4 order.
- [Frozen S evidence comparison](evidence-hash/metrics.md) uses 50 questions and 11,851 eligible turns. The vector channels share deterministic hash vectors. [Manifest](evidence-hash/manifest.json) · [Latency](evidence-hash/latency.json)
- [Local semantic smoke](evidence-bge-six/metrics.md) uses the first six frozen questions, one per question type. Both backends share 384-dimensional BGE embeddings. [Manifest](evidence-bge-six/manifest.json) · [Latency](evidence-bge-six/latency.json)

## Interpreting the evidence runs

The 50-question run scores 46 answerable questions, with four abstentions included in query latency. At k=10, lexical, hybrid and Mem0 raw retrieved at least one relevant session on 36, 32 and 24 of the 46 questions respectively. The shared hash vectors give a reproducible offline control. The recent-turns baseline hit on 12 of 46.

In the six-question BGE run, memfxxk vector and Mem0 raw produced the same order of the top ten turns for all six questions. Both found at least one labeled turn on five questions. Hybrid also found at least one labeled turn on five and found all labeled turns on two, compared with four for the vector-only backends. This small sample is a prompt to inspect fusion before choosing the default for the full S run.

The local semantic run used `fastembed==0.7.4` on CPU with four threads and the `qdrant/bge-small-en-v1.5-onnx-q` snapshot `aa8f8b060edb00e03bfdd08813a2949946c8ba55`. The ONNX file SHA-256 is `51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431`. Mem0 OSS 2.2.1 indexes the same formatted raw turns with model-based extraction and reranking disabled. Query latency is a single-run observation on this local stack. Hybrid runs after vector and can reuse memfxxk's query embedding cache. The semantic run reuses document embeddings during Mem0 preparation.

Full S-scale retrieval, real-model QA, the scale latency target, and a week of coding-agent usage through MCP remain M1 exit checks.

Run the QA smoke test with `uv run pytest tests/integration/test_t07_qa.py`. Follow the [developer manual](../../../docs/developer-manual.md#run-evaluation) for Oracle retrieval commands and use the [retrieval manifest](oracle/manifest.json) to reproduce its embedding dimensions and model label.
