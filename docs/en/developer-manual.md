# memfxxk Developer Manual {#memfxxk}

The [roadmap](https://github.com/shengming-zhou/memfxxk/blob/main/docs/roadmap.md) tracks task dependencies. The [architecture](https://github.com/shengming-zhou/memfxxk/blob/main/docs/architecture.md) defines the interface, scope, and data contracts. See the [user manual](https://shengming-zhou.github.io/memfxxk/en/user-manual/) to start the service and create a key.

## Local development and testing {#local-development}

```sh
docker compose up -d --wait db
uv sync --locked
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Compose creates `memfxxk` and `memfxxk_test` on the same PostgreSQL 17 instance, with pgvector enabled in both databases. Set `MEMFXXK_TEST_DATABASE_URL` to use a different test database. Integration tests apply migrations in isolated temporary schemas and clean up application tables after each test. To run the unit tests alone, use `uv run pytest -m "not integration"`.

## Service boundaries {#service-boundaries}

- The HTTP layer parses the bearer key and scope headers. Application operations go through the `BoundService` returned by `MemoryService.for_request(principal, ctx)`.
- The key determines `org_id`. The caller can set `X-Memfxxk-User`, `X-Memfxxk-Agent`, and `X-Memfxxk-Project` to specify the scope. `X-Memfxxk-Actor` accepts a JSON object such as `{"type":"user","id":"u1"}`.
- The service uses SQLAlchemy Core and Alembic. Scoped queries use `visible_filter`. Missing records and records outside the caller's scope both return 404.

T00 passed 18 tests, including migrations in both directions against PostgreSQL, the scope matrix, and authentication. Starlette TestClient currently emits an httpx deprecation warning.

## Event ingest and indexing {#ingest-and-indexing}

- `BoundService.ingest` checks scope and idempotency, allocates the session sequence, inserts the original event, rebuilds the turn, and queues embedding work in one transaction. Batches that touch multiple sessions acquire their transaction locks in a consistent order to reduce deadlocks.
- Events within a session can have a narrower scope than the session. All events within a turn share a single scope so the derived index follows the source visibility rules. Scope conflicts on visible turns return 409. Sessions and events outside the caller's scope return 404.
- `memfxxk.text` handles tokenization and PostgreSQL literals. `memfxxk.embedding` provides embedding providers, the cache, and index expressions. Hybrid retrieval reuses `embedding_expression` and `embedding_model_filter` for vector retrieval.
- The worker claims jobs with `FOR UPDATE SKIP LOCKED`. It locks the record and rechecks its generation and the job lease before writing. Turn updates invalidate the old vector in the same transaction. `jobs.drain` runs the same job-handling logic used in production.

T01 passed 45 tests covering concurrent sequence allocation, duplicate events, scope isolation, Chinese text and code identifiers, caching, retries, stale writes, and the HNSW query plan in PostgreSQL.

## M1 implementation and checks {#m1-implementation}

- T02–T03 combine lexical, vector, and important-memory candidates. Users can save, mark, link, revise, or deactivate memories directly.
- T04–T05 expose documents, sessions, and context through synchronous and asynchronous Python clients, the CLI, and MCP. HTTP and evaluation adapters share tool definitions and output limits.
- T06–T08 provide LongMemEval retrieval and QA, the A01–A13 regression suite, and a developer-agent demo. Run `uv run pytest -m slow` for the larger test corpus latency check.

The local scale test used 250,000 synthetic turns on PostgreSQL 17 with pgvector 0.8.7. After warmup, 128 sequential hybrid queries with 64-dimensional test vectors recorded 122.68 ms p50 and 162.15 ms p95, above the 100 ms gate. The timings cover database queries and fusion, excluding query embedding and HTTP. This benchmark runs separately from routine tests. Real semantic retrieval, concurrent load, and sustained use by a real coding agent have separate acceptance checks.

```bash
uv run pytest -m scale -s -q tests/integration/test_retrieval.py::test_search_performance_250k
```

## M2 integration and verification {#m2-harness-and-verification}

- Upgrade the database with `uv run alembic upgrade head`. Context building enforces `max_tokens` on the complete memory bundle and retains individual section caps. The reference harness counts the whole input and reserves output space before calling the model, then counts again after tool results.
- MCP exposes the model's memory tools by default. Set `MEMFXXK_MCP_ENABLE_HOST=true` for the harness process to call `events_append`, `session_state`, `session_events`, `context_build`, and the two compaction tools. Give this tool set to the harness. Enable management with `MEMFXXK_MCP_ENABLE_MANAGE=true` and use the `memory-use-v1` prompt.
- Without a configured model, compaction keeps original excerpts based on importance and recency. To use a model, set `MEMFXXK_M2__MODEL__BASE_URL`, `MEMFXXK_M2__MODEL__MODEL`, and `MEMFXXK_M2__MODEL__API_KEY`. With `MEMFXXK_M2__EXTRACT_FACTS=true`, the worker proposes facts from closed turns and checks source events, its job lease, and the record generation. Facts take effect after review. Drafts expire after 24 hours by default.
- Search returns per-stage latency and context returns token use and dropped references. Worker logs include fact extraction token use. Run the command below for replay, scope, deletion, and paired graph checks. The [graph pilot decision](/decisions/0001-m2-graph-pilot) records the methods and limits.

```bash
uv run pytest -s tests/integration/test_m2_memory.py tests/integration/test_m2_benchmark.py tests/integration/test_cli_mcp.py
```

The 512-token LongMemEval S replay used four knowledge-update and four temporal-reasoning questions, with around 98,000–103,000 source tokens per question by the simple counter. The raw tail and rolling summary preserved a complete annotated message for 0/8 questions. Keyword and hash-vector retrieval found annotated evidence in the top eight for 4/8 questions. After packing, full entries and query-focused excerpts each preserved at least one complete annotated message for 3/8 questions. The replay measured evidence retention with model answering disabled. [LongMemEval](https://github.com/xiaowu0162/LongMemEval)

```bash
uv run pytest -s -m slow tests/integration/test_m2_long_replay.py
```

### Paired QA on 30 questions {#m2-paired-qa}

`eval/suites/longmemeval-s-m2-paired30.json` pins the LongMemEval S checksum and ten questions each for knowledge update, temporal reasoning, and multi-session. Every arm retrieves eight candidates and uses the same 1,500-token memory budget and 384-token answer cap. The summary arm reserves room for two local rolling compactions. The graph arm builds cross-session links from rare repeated names in raw user messages and follows two hops. Questions are used for retrieval and answering. Evaluation labels are used to measure evidence and score answers.

Start the local BGE server and set the embedding endpoint as described below. The `prepare` phase creates a separate PostgreSQL schema in the test database, shares the raw corpus and vectors across arms, and saves evidence as each question completes. Set `MEMFXXK_EVAL__GEN__*` for `gpt-5.4-mini` before running `score`. The judge uses the same model. Unfinished phases can resume in the same output directory.

To compare answer models, copy `prepared.jsonl` and `manifest.json` to a new results directory. Update `answer_model` in the manifest and configure the generation endpoint. Keep `judge_model` and set `MEMFXXK_EVAL__JUDGE__*` to reuse the same evaluation model. Both runs use the same frozen contexts.

```bash
uv run python eval/run_m2_paired.py prepare --output eval/results/m2-paired30
uv run python eval/run_m2_paired.py score --output eval/results/m2-paired30
```

`diagnostics.jsonl` records retrieval misses, packed evidence, and truncation. `metrics.json` reports paired wins and losses, p50/p95, and separate answer and judge tokens. Complete evidence means that the whole annotated message is present in the supplied text. A partial message may still support a correct answer.

The graph packing replay used the same 30 questions, BGE semantic embeddings, and a 1,500-token cap. Raising the graph budget from 0 to 350 packed graph evidence in 19/19 eligible questions, up from 0/19. Retrieval still found annotated evidence for 27/30. The number of questions with at least one complete annotated message packed went from 21/30 to 18/30, while packing all complete annotated messages went from 7/30 to 9/30. At a graph budget of 350 tokens, memory processing p50/p95 was 177/266 ms for hybrid, 174/238 ms with summaries, and 199/379 ms with graph evidence. This replay measured evidence and made zero answer or judge calls. The default remains 0.

```bash
uv run python eval/run_m2_paired.py prepare --graph-evidence-tokens 350 --output eval/results/m2-paired30-graph350
```

## M3 procedural memory {#m3-procedural-memory}

`POST /v1/lessons` and the sync and async SDK methods `save_lesson` share `SaveLessonRequest`. The record uses the current request scope and the existing `lesson` type. It stores a user-confirmed text revision, and the audit event records the creation arguments. Failure and verification events must be visible tool results. Session sequence establishes their order within a session, and event time establishes it across sessions. Retrying with the same idempotency key and arguments returns the original creation version.

Lessons use existing search, versioning, and embedding jobs. Both source events are kept in `source_event_ids`, with their roles identified in `source_pos`. Deleting either source removes all lesson versions. Hosts can load lessons through `search_filters.types`, and users can pin them. The host decides how to apply the procedure.

The regression uses real PostgreSQL and local CSV data to exercise failure, verification, and reuse in a later session. It covers API, SDK, MCP, scope, and deletion. AMA-Bench and second-run success with a real agent remain M3 acceptance checks.

```bash
uv run pytest tests/integration/test_m3_lessons.py tests/integration/test_cli_mcp.py tests/integration/test_sdk_tools.py
```

## M3 Markdown workspaces {#m3-markdown-workspace}

`GET /v1/workspace` and SDK `export_workspace` read documents from the exact request scope and retrieve the two evidence events for each selected lesson. Export uses a consistent database snapshot. The CLI writes UTF-8 content under `documents/` and `lessons/`. The manifest stores revision identifiers, purpose, load policy, and the original tool results.

`POST /v1/workspace` and SDK `import_workspace` import documents and lessons in one transaction. Documents use the existing revision checks, section splitting, and indexing. An explicit update requires the exported document ID and revision to match the destination. Lessons retain original IDs and receive new local event references. Deleting a local source event removes its dependent lesson through the existing deletion path. The source and destination retain their own data.

```bash
uv run pytest tests/integration/test_workspace.py tests/integration/test_cli_mcp.py tests/unit/test_workspace_files.py
```

## M3 project credentials and MCP harness {#m3-project-harness}

Migration `0003` adds project grants. Authentication reads the bound project, user, optional agent, and access level. `BoundService` binds request scope and audit identity. Project credentials can read visible inherited data. Project write checks enforce the target boundary. Downgrading revokes project credentials. Existing host keys continue to support trusted harnesses acting on behalf of users.

Project credentials can deactivate and reactivate memories. Administrators or authorized hosts can permanently delete data. Project credentials also need access to the audit event that created a relation before removing or reusing it. Session IDs are unique within an organization. Hosts can create sessions with UUIDs and keep the ID to resume later.

Migration `0005` isolates idempotency keys by the project credential's project, user, and agent. A reissued credential for the same identity can retry earlier writes. Existing host keys keep organization-wide idempotency. Downgrading removes project retry metadata and retains the source events.

`examples/mcp_harness.py` uses MCP over stdio and an allowlist to keep model search and management calls separate from harness-controlled loading, compaction, and capture. The harness resumes existing state using the same session ID and records tool results, roles, and original text by turn.

Graph evidence shares the original evidence and whole-window limits. The graph lane packs up to `graph_evidence_tokens` and returns unused capacity to ordinary evidence, which is rendered first. The field defaults to zero.

```bash
uv run alembic upgrade head
uv run pytest tests/integration/test_project_credentials.py tests/integration/test_mcp_harness.py tests/integration/test_context_graph_budget.py
```

## M3 vector backend and model gateway {#m3-vector-and-gateway}

To select Qdrant, set `MEMFXXK_SEARCH__VECTOR_BACKEND=qdrant` and configure the endpoint and deployment-specific collection prefix. PostgreSQL checks candidate scope and record generation before the Qdrant query. Search falls back to pgvector when the replica lags, the service is unavailable, or the candidate scope exceeds its limit. After migration `0004`, use these commands to create the index, queue existing embeddings, and run the worker. When moving endpoints, keep a worker on the old endpoint to finish queued deletions.

```sh
uv run alembic upgrade head
uv run memfxxk index ensure
uv run memfxxk index reembed
uv run memfxxk worker
```

Use `memfxxk index retry-qdrant` to retry tracked replicas after Qdrant recovers. The default backend is pgvector.

Before downgrading to `0003` or earlier, stop workers that write Qdrant replicas and remove the deployment's dedicated collections, including collections left at old endpoints or prefixes. After confirming remote cleanup, clear the PostgreSQL `qdrant_replicas` table and run the Alembic downgrade. Migration `0004` stops the downgrade while the registry contains placements.

Configuring a model upstream enables `POST /v1/chat/completions` through the existing `BoundService`. Set the memory budget with `MEMFXXK_GATEWAY__MEMORY_TOKENS`. The model receives retrieved memory as quoted reference data in the current user message. A successful response records the original user message and assistant reply. The idempotency key deduplicates storage. Upstream requests can be sent again. MCP handles proactive model tools and the harness lifecycle.

GitHub CI runs tests and Ruff against real PostgreSQL. A separate workflow publishes the bilingual manuals. [uv and GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)

## T16 P0 task model calls {#t16-llm-routing}

Migration `0006` adds `llm_calls`. Compaction and fact extraction use `memfxxk.llm.runner.TaskRunner`. Task-specific settings override `MEMFXXK_LLM__DEFAULT__*`, with legacy `MEMFXXK_M2__MODEL__*` as a compatibility fallback. Independent evaluations retain `MEMFXXK_EVAL__GEN__*` and `MEMFXXK_EVAL__JUDGE__*`. Model settings cover `MODEL`, `BASE_URL`, `API_KEY`, `TEMPERATURE`, `MAX_OUTPUT_TOKENS`, `TIMEOUT`, `MAX_RETRIES`, and `RESPONSE_FORMAT`.

Select `prompt`, `json_object`, or `json_schema` for `RESPONSE_FORMAT`. The client validates structured output and requests one repair after validation fails. When both attempts fail, the task writes no derived data. `llm_calls` tracks task, prompt version, model, scope, associated job or request ID, input hash, status, and usage. Debug payloads default to disabled. With debug enabled, payloads are stored for calls with visible original sources and cleared when a source is erased.

Set `MEMFXXK_LLM__DAILY_LIMITS` to a JSON array of scopes and `tokens`. Each matching organization-level or narrower limit considers UTC-day usage and concurrent reservations. Over-budget synchronous calls return `429 llm_budget_exceeded`. Background jobs wait until the next UTC day without consuming a failure attempt. Reservations use estimated input and a bounded output. Reconciliation uses reported provider usage when available. Verify with `uv run pytest tests/unit/test_llm_routing.py tests/integration/test_llm_tasks.py`.

## T16 P1 structured compaction {#t16-structured-compaction}

### Upstream behavior {#compaction-upstream}

Codex submits conversation history to a model, prepends a fixed summary prefix, and keeps recent user messages within a 20,000-token limit. On a context overflow, it removes the oldest entry and retries. Codex 0.172.0 uses 90% of the model window as its default auto-compaction limit and honors lower configured limits. MemGPT asks a model to summarize evicted messages together with the previous summary, can warn the model at a soft threshold, and keeps evicted history searchable. [Codex prompt](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/prompts/templates/compact/prompt.md) [Codex compactor](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/compact.rs) [Codex threshold](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/protocol/src/openai_models.rs) [MemGPT §2.2](https://arxiv.org/html/2310.08560#S2.SS2)

### Implementation {#compaction-implementation}

Migration `0007` adds `method`, `checkpoint`, and `retained_user_event_ids` to `compactions`. With a `compact` task model configured, `prepare_compaction` sends the previous checkpoint and newly covered closed events to `compact-v1`. The input includes event order, roles, and explicit marks. Every claim must cite source UUIDs. The service drops a claim containing an out-of-range UUID and logs the number of dropped claims. The checkpoint and its rendered summary fit the requested `target_tokens`. Commit rechecks the generation and revision before publishing the new working summary.

Without a configured model, the method is `extractive_fallback`. Pre-migration rows show `legacy`. When validation fails on a configured model, the runner requests one repair. Two failed attempts return `llm_invalid_output`. Compatible gateways can use `MEMFXXK_LLM__TASKS__COMPACT__RESPONSE_FORMAT=json_object`. Long inputs may need a higher `MAX_OUTPUT_TOKENS`. Limit recent verbatim user messages with `MEMFXXK_M2__RECENT_USER_TOKENS`. The default is 1,024 and zero disables retention. Assembly still applies the working and total memory budgets.

### Differences from upstream {#compaction-differences}

Hosts can schedule compaction when `pressure_ratio` exceeds `MEMFXXK_CONTEXT__PRESSURE_THRESHOLD` and `pressure` becomes `warning`. The ratio uses the effective budget of the request and its enabled sections. The reference harnesses build context, compact an eligible closed prefix, and rebuild context. If the input exceeds `MEMFXXK_M2__COMPACT_INPUT_TOKENS` (128,000 by default), the service returns `compaction_input_too_large`. The host can choose a smaller compaction boundary. Checkpoint claims and retained messages are selected as whole entries. Persisted context bundles hold references and counts.

### Release date example {#compaction-example}

The first preparation sends an empty previous checkpoint, the closed events, and a target token count. The model puts “release Friday” and its event UUID in `latest_decisions`. Committing the draft creates the first `summary` version in `records`. After the user changes the date, the second preparation sends the first checkpoint and the new events, including the correction. The latest decision says Tuesday and cites the correction UUID. Committing publishes version 2. The old version and original conversation remain readable. A 66-turn run with DeepSeek Flash reconnected the service between the two compactions and checked both the loaded decision and its source.

### Live model comparison {#compaction-live-evaluation}

With the frozen dataset, a dedicated PostgreSQL test database, a semantic embedding server, and both models ready, run `uv run python eval/run_m2_paired.py all --structured-summary --output eval/results/t16/p1`. Add `--prepare-workers 4` to use four independent database schemas. All arms share the same 30 questions, 1,500-token memory budget, answer model, and judge. The two summary arms share a summary cap and compaction boundaries. They compare the loaded summary text, with verbatim recent-message retention covered by a separate workflow test. The diagnostics count verbatim gold evidence and gold citations from the checkpoint separately. The metrics include answer, judge, and compaction usage.

The 2026-11-27 run replayed 6,667 turns with BGE-small-en-v1.5 embeddings, `deepseek-flash` compaction, and `gpt-5.4-mini` answering and judging. Compaction used `json_object` mode and a 4,000-token output cap. Each arm retrieved top 8. Both summary arms targeted 240 tokens and allocated up to 320 tokens to load the summary. The run directory is `eval/results/t16/p1-live`.

| Strategy | Correct | Loaded at least one complete gold message | Answer end-to-end p50 / p95 | Answer input / output tokens | Judge input / output tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Hybrid retrieval | 7/30 | 21/30 | 1.23 / 1.79 s | 47,594 / 756 | 3,152 / 126 |
| Hybrid + extractive summary | 7/30 | 18/30 | 1.09 / 1.40 s | 47,505 / 622 | 3,018 / 120 |
| Hybrid + model summary | 8/30 | 18/30 | 1.20 / 1.52 s | 47,440 / 699 | 3,095 / 120 |

Each arm missed gold evidence at retrieval for four questions and retrieved it without loading it for five. Both summary arms loaded only partial gold messages for three more questions. The model summary cited a gold source on five questions and reproduced zero gold messages verbatim. It gained two answers and lost one compared with the extractive summary. Temporal reasoning went from 2/10 to 3/10. Knowledge updates stayed at 5/10 and multi-session questions at 0/10. Compaction remains opt-in through a model setting, with hybrid retrieval the default for question answering.

The model made 60 compaction calls using 4,125,762 input and 83,614 output tokens. The two compactions per question took p50 / p95 of 16.88 / 23.22 seconds, compared with 0.55 / 0.78 seconds for extractive summary. The table's answer latency excludes this build time. The observed cost per 1,000 turns was about 618,833 input and 12,541 output tokens. Including answering and judging, the formal run used 4,363,623 tokens. Separate gateway diagnostics are outside that total.

## T16 P2 fact write strategies {#t16-fact-strategies}

`MEMFXXK_STRATEGIES__FACTS` selects `off`, `key_proposal`, `mem0_classic`, or `mem0_additive`. Without an explicit setting, `MEMFXXK_M2__EXTRACT_FACTS` still controls the original review workflow, which defaults to off. Apply the same configuration to ingestion and workers. Both Mem0 strategies process closed turns and validate job ownership, source generation, visibility, and the target version before writing.

### Mem0 classic {#mem0-classic}

#### Upstream behavior {#mem0-classic-upstream}

The paper extracts facts using a session summary, ten recent messages, and the newest message pair. For each candidate it retrieves ten similar memories. A second model call decides ADD, UPDATE, DELETE, or NONE. The earlier OSS implementation maps temporary IDs to stored IDs, removes vectors on DELETE, and records old text, new text, operation, and deletion in SQLite history. [Mem0 §2.1](https://arxiv.org/html/2504.19413#S2.SS1) [v0.1.118 writes](https://github.com/mem0ai/mem0/blob/v0.1.118/mem0/memory/main.py) [History table](https://github.com/mem0ai/mem0/blob/v0.1.118/mem0/memory/storage.py)

#### Implementation {#mem0-classic-implementation}

`m2/mem0.py` reads the closed turn, the previous ten messages in the same session, and any earlier committed summary. Configure the `extract` and `reconcile` tasks. A new statement must cite the current turn. Cosine search returns up to ten visible current and valid facts per candidate. Newly written, unindexed facts participate with on-demand embeddings. An UPDATE or DELETE can only use an integer from that candidate's retrieval results. UPDATE checks `old_memory` and the current record version.

#### Differences from upstream {#mem0-classic-differences}

UPDATE adds a version in `records`. DELETE writes an inactive version with a zero-length validity interval under the same memory ID. Valid and recorded time constraints preserve historical reads. Explicitly saved, confirmed, and marked facts get review proposals. Migration `0008` adds `operation` and `base_version` to `fact_proposals` so approvals check the target version. Downgrade preserves those fields and the review status in JSON, temporarily rejecting pending actions. Upgrade restores their status.

#### Example {#mem0-classic-example}

“Atlas uses the Paris hub” yields a candidate and an ADD, which creates version 1. A move to Lyon produces a new extracted statement. The next reconciliation receives Paris at ID `0` and returns an UPDATE with the replacement text and exact Paris text. Automatic facts get version 2. If the user confirmed the Paris fact, version 1 stays in effect until the proposal is approved.

#### Evaluation results {#mem0-classic-evaluation}

Run `uv run python eval/run_t16_facts.py --output eval/results/t16/p2-oracle`. The suite freezes three knowledge-update and three temporal-reasoning questions. All arms use the same LongMemEval Oracle histories, top 8, and a 1,500-token memory budget. The dataset provides the relevant sessions. This test measures changes in fact writes and answers. A subsequent comparison can measure retrieval with the full distractor history.

The final run used `deepseek-flash` for extraction and reconciliation, `gpt-5.4-mini` for answers and judging, and BGE-small-en-v1.5 embeddings. Each arm replayed 51 turns. G3 answered 2/6 and classic 3/6. Knowledge updates improved from 2/3 to 3/3, with both arms at 0/3 for temporal reasoning. Classic loaded a fact with a gold source on three questions. Its 101 write calls used 181,548 input and 10,224 output tokens. Per-question ingest, write, and index p50 / p95 latency was 24.52 / 33.05 seconds, compared with G3 at 1.30 / 2.55 seconds. Classic retrieval and assembly p50 / p95 was 64 / 153 milliseconds, compared with G3 at 190 / 245 milliseconds.

### Mem0 additive {#mem0-additive}

#### Upstream behavior {#mem0-additive-upstream}

OSS v3 retrieves ten existing memories with an embedding of the current messages. One model call sees recent messages, existing memories, and the new turn. The new items are embedded and written in batches. The model attributes statements to users or assistants and can link to existing memories. Duplicates are removed and existing records remain available. [Mem0 prompt](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py) [Mem0 writes](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)

#### Implementation {#mem0-additive-implementation}

One `extract` call supplies additions. The worker checks speaker attribution and current-turn sources and stores the normalized content hash, entity labels, and `attributed_to` in fact metadata. Valid linked IDs create rows in `memory_relations`. Entity matching normalizes Unicode, case, and whitespace, then checks cosine similarity against retrieved entity labels at a 0.95 threshold. Adopting a canonical alias retains its source dependency so erasure also removes the affected derived record.

#### Differences from upstream {#mem0-additive-differences}

`MEMFXXK_STRATEGIES__MEM0_ADDITIVE_RANKING=true` calculates BM25 over additive facts that the hybrid search retrieved and adds entity and effective-time weights. The original PostgreSQL full-text and vector channels supply the candidates. Both link endpoints are checked again for visibility and active status. Erasing an original source uses existing deletion propagation to remove the affected fact and its links.

#### Example {#mem0-additive-example}

The user first says Atlas uses the Paris hub and later switches to Lyon. The extractor sees Paris with a temporary ID and returns the Lyon statement, user attribution, the link ID, and entity `Atlas`. Both facts remain available. A relevant query favors the more recent effective time, and the link leads back to the Paris sources.

#### Evaluation results {#mem0-additive-evaluation}

Uses the [same frozen suite](#mem0-classic-evaluation) with additive fact weighting enabled. The final run answered 3/6, with 2/3 knowledge updates and 1/3 temporal reasoning. It gained two answers and lost one against G3, and loaded facts with gold sources on all six questions. There were 56 write calls, including five initial validation failures followed by repair. All 51 write jobs finished. Write usage was 174,095 input and 25,331 output tokens. Ingest, write, and index p50 / p95 was 25.92 / 45.41 seconds. Retrieval and assembly p50 / p95 was 70 / 141 milliseconds. Scores varied between the two runs. Both strategies remain opt-in. The final run used 419,464 tokens in total. The manifest and per-question diagnostics are in `eval/results/t16/p2-oracle-validated`.

## T16 P3 temporal knowledge graph {#t16-temporal-graph}

### Upstream behavior {#temporal-graph-upstream}

Graphiti extracts entities, retrieves candidates using text and name vectors, then asks a model to resolve identity. It extracts relationships with temporal bounds and checks existing edges for duplicates and contradictions. Same-pair edges supply duplicates, and other relevant edges supply invalidation candidates. A later contradiction closes the earlier interval and records when the service learned that fact. The paper uses four recent episodes. The checked code sets the default lookup and retrieval limits to ten, similarity to 0.6, and MMR λ to 0.5. [Zep paper](https://arxiv.org/html/2501.13956) [Search configuration](https://github.com/getzep/graphiti/blob/2a4f079ccedf32c36693e503d0a1cdfa2821d223/graphiti_core/search/search_utils.py) [Edge updates](https://github.com/getzep/graphiti/blob/2a4f079ccedf32c36693e503d0a1cdfa2821d223/graphiti_core/utils/maintenance/edge_operations.py)

### Implementation {#temporal-graph-implementation}

`MEMFXXK_STRATEGIES__GRAPH=graphiti` schedules `build_entity_graph` for closed turns. The worker reads raw events and four earlier turns, extracts entities, resolves them in a batch if candidates exist, extracts edges, resolves each edge that has candidates, and summarizes entities in a batch. All five tasks have configurable models, versioned prompts, and structured validation. The code is in `m2/temporal_graph/`. Migration `0009` adds `entities`, `entity_edges`, and `entity_mentions`. A transaction validates the turn and graph generations and the job lease before writing. Erasure removes supporting evidence and undoes any invalidation based on that erased evidence.

### Differences from upstream {#temporal-graph-differences}

PostgreSQL stores the graph. Retrieval combines terms, cosine similarity, and adjacency for entities or edges, with BM25 scoring on the full-text shortlist. Select `rrf`, `mmr`, `node_distance`, or `episode_mentions` through `MEMFXXK_STRATEGIES__GRAPH_RERANKER`. Duplicate detection compares edges with the same endpoints and relation type. State changes can also compare different objects with the same subject and relation type. `graph_hops` sets expansion depth. `graph_evidence_tokens` budgets assembly. Search hits can show `graph_fact`, and the persisted bundle keeps only `graph_edge_id`, original turn references, and counts. Existing expansion remains available as `graph=links`.

### Example {#temporal-graph-example}

On January 1, Rae says they live in Beijing. The worker extracts the person, city, and `LIVES_IN` relationship with a raw citation. On January 3, Rae Tang says they moved to Shanghai. Resolution maps both names to the same entity. The new edge starts on January 3. The old edge's `invalid_at` becomes January 3, and `expired_at` records when the service learned about the change. January 2 and January 4 queries return Beijing and Shanghai respectively. Erasing the move restores the earlier interval.

### Evaluation results {#temporal-graph-evaluation}

Run `uv run python eval/run_t16_graph.py --output eval/results/t16/p3-oracle`. All arms share the same LongMemEval Oracle histories, top 8, and a 1,500-token memory budget. Both graph arms reserve 300 tokens within that total and expand at most two hops. The frozen suite contains three multi-session and three temporal-reasoning questions with at least two sessions each. The links arm builds shared-name links from the original conversation.

Each arm replayed 60 turns using BGE-small-en-v1.5 embeddings, `deepseek-flash` graph writes, and `gpt-5.4-mini` answering and judging. Hybrid, links, and temporal graph scored 1/6, 1/6, and 0/6. The two correct answers were multi-session. All arms scored 0/3 on temporal reasoning and loaded at least one gold source on five questions. Link and temporal expansions loaded evidence on two and five questions respectively, with zero questions gaining a gold source from those expansions. The temporal arm included edge references in loaded results on all six questions.

All 60 temporal graph jobs finished. There were 239 model calls, including five initial validation failures followed by repair. Writes used 870,987 input and 89,513 output tokens. Per-question write build p50 / p95 was 68.77 / 175.23 seconds. Retrieval and assembly p50 / p95 was 208 / 253, 80 / 100, and 124 / 183 milliseconds respectively, with cold-start costs concentrated in the first arm. Write usage extrapolates to about 14.52 million input and 1.49 million output tokens per 1,000 turns from this replay. Including answers and judging, the run used 991,779 tokens. Link expansion remains the default graph strategy.

## T16 P4 A-Mem note network {#t16-amem}

### Upstream behavior {#amem-upstream}

The paper generates keywords, tags, and a description, then embeds them with the note content. It retrieves vector neighbors, asks a model to choose useful links, and considers evolving each neighbor's description. Appendix B.3 lists `should_evolve`, `actions`, `suggested_connections`, `tags_to_update`, `new_context_neighborhood` and `new_tags_neighborhood`. The action examples differ from the prose. The MIT implementation handles `strengthen` and `update_neighbor`. [A-Mem §3 and Appendix B.3](https://arxiv.org/pdf/2502.12110) [Implementation](https://github.com/agiresearch/A-mem/blob/ceffb860f0712bbae97b184d440df62bc910ca8d/agentic_memory/memory_system.py)

### Implementation {#amem-implementation}

`MEMFXXK_STRATEGIES__NOTES=amem` schedules `build_amem_note` after normal embedding for `note`, `fact`, and `lesson` records. The worker makes one `note_enrich` call, one `note_link` call when neighbors exist, and a `note_evolve` call per eligible neighbor, up to ten. Migration `0010` adds `amem_revisions` and `amem_links` for enhanced vectors, revision history, sources, and call IDs. `records.metadata.amem` holds current derived attributes. Vector search fuses enhanced matches and reports `score.note_rank` and `score.note_similarity`. Existing expansion can use relations with `purpose=auto_link`.

### Differences from upstream {#amem-differences}

Evolution changes derived attributes and preserves original text and manually assigned tags. It skips explicitly saved and confirmed neighbors. Link and evolution results use separate schemas. The write transaction checks record generations, prior attributes, job ownership, and raw evidence. Erasing a source removes dependent revisions and automatic links, and restores a surviving attribute revision when available. Inspect attribute history with `GET /v1/memories/{id}/note-attributes/revisions`. Rollback uses `POST /v1/memories/{id}/note-attributes/rollback` with `base_revision` and `target_revision`. The API returns a new attribute revision, a 404 for an unavailable target, or `409 note_revision_conflict` for stale revisions.

Enhanced search uses the shared `build_context` candidate interface. Its bundle references carry `ref_id`, `record_type`, relevance `score`, full `candidate_tokens`, and loaded `tokens`. `decision` and `decision_reason` explain selection, truncation, or removal, including duplicates and items already held by the host. Stored bundles contain references and this metadata.

### Example {#amem-example}

“Atlas nightly imports used Paris” stores the original note and first retrieval description. A later note records the move to Lyon. `note_link` connects the two notes and `note_evolve` revises the older note's derived context to describe the historic Paris configuration. The original text and its content version stay available. Rolling attributes back to revision one creates revision three. `memory_get` and the HTTP or SDK `get_memory` expose `note_attributes`.

### Evaluation results {#amem-evaluation}

Run `uv run python eval/run_t16_notes.py --output eval/results/t16/p4-oracle-shared-budget`. The frozen LongMemEval Oracle set contains two multi-session and two knowledge-update questions. Both arms get identical notes derived verbatim from the conversation and a 1,500-token budget. The A-Mem arm uses enhanced embeddings and one-hop automatic links. Graph and ordinary evidence share that budget.

The final run used BGE-small-en-v1.5 embeddings, `deepseek-flash` for note processing, and `gpt-5.4-mini` for answering and judging. Both arms answered 4/4 and loaded gold sources for all four questions. A-Mem loaded enhanced notes for each question, with zero additional graph expansion results loaded. All 40 note jobs finished. The 258 model calls included one initial validation failure and its repair. Usage was 1,116,819 input and 8,921 output tokens, or 1,139,453 tokens including answering and judging. Per-question write p50 / p95 was 75.16 / 92.89 seconds, and retrieval and assembly was 100 / 135 milliseconds. The baseline measured 1.92 / 2.59 seconds and 178 / 196 milliseconds respectively. Scaled from the replay, 1,000 turns would use about 27.92 million input and 223,000 output tokens for writing. A-Mem is off by default.

## T16 P5 Editable memory blocks {#t16-memory-blocks}

### Upstream behavior {#memory-block-upstream}

Letta provides a label, purpose, value, character limit, and `read_only` flag with each block. The archived Python implementation exposes `core_memory_append(agent_state, label, content)`, `core_memory_replace(agent_state, label, old_content, new_content)`, and `rethink_memory(agent_state, new_memory, target_block_label)`. The generic `memory` tool also supports viewing, editing, and inserting by path. Background dreaming can run after a configured number of steps or after compaction, with an optional review pass. [Letta's Apache-2.0 Python source](https://github.com/letta-ai/letta/blob/archive/letta/functions/function_sets/base.py) [Dreaming documentation](https://docs.letta.com/configuration/memory)

### Implementation {#memory-block-implementation}

Migration `0011` adds `memory_blocks`, `block_revisions`, and `block_proposals`. Blocks have a current revision pointer and a monotonically increasing revision counter. Every adopted edit creates a revision with an actor and source events. Explicit initial values are recorded as `user_op` events. The core service enforces organization, user, agent, and project visibility. The strategy controls context loading and background consolidation. Explicit API operations remain available.

| Operation | HTTP | SDK and MCP |
| --- | --- | --- |
| Create | `POST /v1/memory-blocks` | `create_block(CreateBlock(...))` |
| List or read | `GET /v1/memory-blocks` or `GET /v1/memory-blocks/{id}` | `list_blocks`, `get_block`, and `memory_block_list`, `memory_block_get` |
| Edit | `PATCH /v1/memory-blocks/{id}` | `edit_block(id, EditBlock(...))` and `memory_block_edit` |
| List proposals | `GET /v1/block-proposals` | `list_block_proposals` |
| Review | `POST /v1/block-proposals/{id}/review` with `{"accept":true}` | `review_block(id, True)` |

An edit carries `base_revision`, `operation`, and `text`. For replacements, pass an `old_text` that matches once. Use `source_event_ids` when citing source events. A stale revision returns `409 block_revision_conflict`. Oversize edits return `422 block_limit_exceeded` with `char_count`, `requested_char_count`, and `limit`. Hidden and unknown blocks both return 404.

Completed turns and adopted compactions can schedule `sleeptime_consolidate`. The worker reads recent originals, the latest eligible summary, and current blocks. It then calls `sleeptime`. Before writing it checks the job claim, source availability, generations, and block revisions. Editable agent blocks update directly, while protected block edits and suggested facts go to review. Forgetting a source removes dependent revisions and proposals and restores the latest surviving block revision.

### Differences from upstream {#memory-block-differences}

Separate tables support optimistic edits, source deletion, and approvals. A host can make a user block visible to multiple agents through existing scopes. Edit permissions are evaluated separately. Consolidation uses one structured model call. Context renders the description, character count, and value in a `<memory_block>` element. The harness places it in the model request. A block uses the same `_Candidate` path as documents, with an ID, type, relevance, token count, and packing decision. The stored bundle contains references.

### Worked example {#memory-block-example}

An agent reads revision 1 of its handoff block and invokes `memory_block_edit` with `{"block_id":"…","edit":{"base_revision":1,"operation":"rewrite","text":"Thursday handoff"}}`. The next harness turn can load revision 2 through `context_build`. For a user block, the same call returns a proposal. The value changes after the owner accepts it. [Reference harness integration test](https://github.com/shengming-zhou/memfxxk/blob/main/tests/integration/test_mcp_harness.py)

### Evaluation {#memory-block-evaluation}

Run `uv run python eval/run_t16_blocks.py --output eval/results/t16/p5-long-dialogue` for the frozen 64-turn, four-question replay with a shared 1,500-token memory limit. It uses BGE-small-en-v1.5 embeddings, `deepseek-flash` for consolidation, and `gpt-5.4-mini` for answering and grading. Both arms answered 2/2 preference and 2/2 rescheduling questions. Both loaded the original supporting message for all four answers. The blocks also captured the preferences and updated commitments.

Four consolidation calls succeeded and spent 12,289 input and 1,321 output tokens, or about 192,016 input and 20,641 output tokens per 1,000 turns at this rate. Total write time was 2.94 seconds for hybrid and 12.80 seconds for blocks. Assembly p50 / p95 was 135 / 171 ms and 69 / 108 ms. Answer p50 / p95 was 850 / 1,421 ms and 614 / 814 ms. Four answers used 3,609 and 4,759 tokens respectively. Including grading and consolidation, both arms spent 22,647 tokens in total. First-call timing and run order affect these small-sample latencies. Automatic consolidation remains off by default.

## T16 P6 MemoryOS tiers {#t16-memoryos}

### Upstream behavior {#memoryos-upstream}

The paper uses seven pages in its recent queue and at most 200 topic segments. A page holds a turn and a chain summary. Moving pages into a segment uses cosine similarity plus keyword Jaccard, with a 0.6 threshold. Heat combines visits, page counts, and `exp(−Δt/10⁷)`. Capacity pressure evicts the coldest segment. Heat above five triggers proposed updates for durable user knowledge, preferences, and agent traits. Retrieval takes the recent pages and ranks pages from the top five matching segments, using ten segment pages for LoCoMo. [MemoryOS paper sections 3 and 4.1](https://arxiv.org/html/2506.06326v1) [Apache-2.0 implementation](https://github.com/BAI-LAB/MemoryOS)

### Implementation {#memoryos-implementation}

Migration `0012` creates `tier_pages` and `tier_segments`. Pages reference the original turn row and generation. A segment keeps its seed embedding, keyword set, summary, source IDs, unpromoted page count, visits, and last access. The full scope determines the write bank, and reads use core visibility rules. A closed turn schedules `tier_project`. Eligible overflow pages move into a segment and schedule `tier_summarize`. A retrieval that crosses the promotion threshold can schedule the same job.

Set `MEMFXXK_STRATEGIES__TIERS=memoryos` and configure the `persona_update` model. The `MEMFXXK_TIERS__SHORT_CAPACITY`, `SEGMENT_CAPACITY`, `SEGMENT_THRESHOLD`, and `PROMOTION_HEAT` settings control thresholds. The worker reads up to 64 of the newest original pages, then asks for a `summary` and optional versioned block proposals. It checks the job claim, segment and page generations, and source visibility before writing. Promotions use P5 review. Forgetting a source removes dependent segments or proposals. Surviving original turns remain reachable through hybrid search.

### Differences from upstream {#memoryos-differences}

We use the paper's 0.6 / 5 defaults. The source repository's ChromaDB example uses 0.7 / 13. A page holds a turn reference, and the segment keeps a model summary. Pending generations can coalesce into one summary for the latest state. Promotion resets counters and keeps original turns searchable. Long-term profiles use P5 blocks provisioned by the host, with approval. Recorded-time queries use the original temporal search path.

Recent and segment hits pass through the shared `_Candidate` evidence pipeline. Bundles keep `strategy=memoryos`, the original turn ID, type, relevance, candidate tokens, and loading decisions. The strategy shares the existing evidence budget.

### Worked example {#memoryos-example}

Several turns mention brewing tea and choosing a water temperature. After turn eight, the first turn moves out of the recent queue. Similar older turns join the tea segment. Once heat exceeds five, `persona_update` reads the source turns and summarizes the tea topic. It may propose that an existing `user_profile` record the user's preference for a light brew. The user can review the sources before accepting. Original turns remain available during review.

### Evaluation {#memoryos-evaluation}

Run `uv run python eval/run_t16_tiers.py --output eval/results/t16/p6-tiers` for the same 1,500-token limit across the frozen 64-turn dialogue and LoCoMo `conv-26`. Put the pinned `locomo10.json` file in `eval/data/`. The script verifies SHA-256 and records answers, evidence, candidate references, model calls, token usage, and timings. [Pinned LoCoMo source](https://github.com/snap-research/locomo/blob/3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376/data/locomo10.json)

The live run used BGE-small-en-v1.5, `deepseek-flash` for writes, and `gpt-5.4-mini` for answering and grading. The long dialogue fell from 4/4 to 3/4, with tiers missing one original preference message. On the 214-turn LoCoMo conversation, results fell from 2/8 to 1/8 and gold source hits from 4/15 to 3/15. Hybrid and tier assembly p95 was 194 / 251 ms for the long dialogue and 229 / 303 ms for LoCoMo. Each tier replay coalesced its writes into one model call, for 21,426 input and 946 output tokens combined, or about 80,475 tokens per 1,000 turns. Answering and grading used 35,984 tokens for a total of 58,356. Live incremental cost depends on how often segment updates run. Tier loading remains off by default.

## Reproduce the baseline {#run-evaluation}

Use a dedicated PostgreSQL database. See `.env.example` for embedding, generation, and judge configuration. Retrieval runs record the dataset SHA, model, metrics by question type, and latency. QA runs also record tokens, tool calls, and model latency by group.

The repository includes Oracle retrieval, a frozen 50-question evidence comparison with deterministic vectors, a six-question local BGE comparison, and a two-question QA smoke run with fake models. [M1 evaluation reports](https://github.com/shengming-zhou/memfxxk/tree/main/eval/results/m1-smoke)

### Compare evidence on a frozen suite {#evidence-comparison}

Apply the migrations to a dedicated evaluation database and ingest the 50-question LongMemEval_S suite. The command validates the dataset SHA-256 and processes questions in suite order. Recent turns, memfxxk lexical/vector/hybrid retrieval, and optional Mem0 OSS use the same time cutoff.

```sh
uv run --locked memfxxk eval longmemeval fetch --dataset s
uv run --locked memfxxk eval longmemeval ingest --dataset s --suite eval/suites/longmemeval-s-smoke.json
uv run --locked memfxxk index ensure
uv run --locked --with mem0ai==2.2.1 memfxxk eval longmemeval evidence --suite eval/suites/longmemeval-s-smoke.json --mem0
```

The evidence run reports session and turn Recall@5/10/20, query p50/p95/p99, and counts of empty results and unmapped hits. Latency includes abstention questions. Unmapped results are reported as missing in source metrics. Mem0 runs on raw turns with `infer=False` to measure source retrieval. This stage requires no generation or judge endpoint.

For real semantic embeddings, start the local CPU service with `uv run --locked --with fastembed==0.7.4 python eval/serve_embeddings.py`. Add `--offline` after the weights are cached. Apply these settings in the shell running ingest and evaluation.

```dotenv
MEMFXXK_EMBEDDING__PROVIDER=openai_compatible
MEMFXXK_EMBEDDING__BASE_URL=http://127.0.0.1:8011/v1
MEMFXXK_EMBEDDING__MODEL=BAAI/bge-small-en-v1.5
MEMFXXK_EMBEDDING__DIM=384
```

Both memfxxk and Mem0 use these settings. Mem0 reuses the vectors for identical source text when preparing the corpus. If you already ingested with another model, run `memfxxk index reembed` to enqueue the current model, then drain the jobs with the same `ingest --suite` command. Evaluation fails when vector coverage is incomplete. For a quick semantic smoke test, `eval/suites/longmemeval-s-embedding-smoke.json` selects the first six questions, one per type.

### Model API smoke test {#model-api-smoke}

`uv run pytest tests/unit/test_t07_llm.py` uses a local HTTP mock to check OpenAI-compatible requests and tool-call continuation. A live run uses `MEMFXXK_EVAL__GEN__BASE_URL` for the API prefix (for example, a URL ending in `/v1`), `MEMFXXK_EVAL__GEN__MODEL`, and `MEMFXXK_EVAL__GEN__API_KEY`. The judge uses the matching `MEMFXXK_EVAL__JUDGE__*` settings.

### Compare answers with frozen evidence {#replay-evidence-qa}

Keep the complete local directory produced by `evidence`, and pass it and the matching suite to QA. Replay reads source turns up to each question's cutoff and uses the same format, top-k limit, and token budget across ranked backends. The evidence report has retrieval latency. The QA report measures context assembly from saved rankings and answer generation. The G4 tool-calling group measures live retrieval during its own run.

```sh
uv run --locked memfxxk eval longmemeval qa \
  --suite eval/suites/longmemeval-s-smoke.json \
  --evidence-run eval/results/ACTUAL-EVIDENCE-RUN \
  --groups G1,recent-turns,memfxxk-hybrid,mem0-raw \
  --k 8 --evidence-budget 1500 --max-answer-tokens 384 --run-name qa-smoke
uv run --locked memfxxk eval longmemeval judge eval/results/qa-smoke
```

### Retrieval and QA commands {#retrieval-and-qa-commands}

```sh
uv run memfxxk eval longmemeval fetch --dataset oracle
uv run memfxxk eval longmemeval ingest --dataset oracle
uv run memfxxk index ensure
uv run memfxxk eval longmemeval retrieval --dataset oracle --run-name oracle
uv run memfxxk eval longmemeval qa --dataset oracle --groups G1,G2,G3,G4 --limit 50 --run-name oracle-qa
uv run memfxxk eval longmemeval judge eval/results/oracle-qa
uv run memfxxk eval report eval/results/oracle-qa
```

To run the official judge, pass `judge --official`, point `MEMFXXK_EVAL__OFFICIAL_REPO` to a separate checkout, and configure credentials for the upstream script. Report official and built-in scores separately.

## Publish the manuals {#publishing}

Changes to the manuals or VitePress configuration on `main` trigger [Publish manuals](https://github.com/shengming-zhou/memfxxk/actions/workflows/docs.yml), which builds and deploys both languages. You can also run the workflow manually in Actions. Update the English edition in `docs/en/` when editing a manual, and keep section anchors consistent across translations.

Run these commands from the repository root with Node.js 24 to verify the build. The generated files are in `docs/.vitepress/dist/`.

```sh
npm ci
npm run docs:build
```

Use `npm run docs:dev` for a live local preview while editing.
