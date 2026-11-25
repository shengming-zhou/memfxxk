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

M1 still requires full LongMemEval_S retrieval, QA with real models, p95 latency on about 250,000 turns, and a week of MCP use with a real coding agent. Those checks gate the start of M2.

## Reproduce the baseline {#run-evaluation}

Use a dedicated PostgreSQL database. See `.env.example` for embedding, generation, and judge configuration. Retrieval runs record the dataset SHA, model, metrics by question type, and latency. QA runs also record tokens, tool calls, and model latency by group.

The repository includes a retrieval report on the official Oracle split with fake embeddings and a two-question QA smoke run with fake models. [M1 evaluation reports](https://github.com/shengming-zhou/memfxxk/tree/main/eval/results/m1-smoke)

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

Both memfxxk and Mem0 use these settings. If you already ingested with another model, run `memfxxk index reembed` to enqueue the current model, then drain the jobs with the same `ingest --suite` command. Evaluation fails when vector coverage is incomplete.

### Model API smoke test {#model-api-smoke}

`uv run pytest tests/unit/test_t07_llm.py` uses a local HTTP mock to check OpenAI-compatible requests and tool-call continuation. A live run uses `MEMFXXK_EVAL__GEN__BASE_URL` for the API prefix (for example, a URL ending in `/v1`), `MEMFXXK_EVAL__GEN__MODEL`, and `MEMFXXK_EVAL__GEN__API_KEY`. The judge uses the matching `MEMFXXK_EVAL__JUDGE__*` settings.

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
