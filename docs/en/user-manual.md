# memfxxk User Manual {#memfxxk}

## Local setup {#local-setup}

Install Python 3.12, uv, and Docker Compose. Run these commands from the repository root. The development database listens on local port 5433.

```sh
docker compose up -d --wait db
uv sync --locked
uv run memfxxk db upgrade
uv run memfxxk keys create --org demo --role host --name local
uv run memfxxk serve
```

The key creation command displays the plaintext key once. Save it in the caller's secret configuration. The service listens on local port 8000 by default. Interactive API documentation is available at `/docs`.

## Check the connection {#check-connection}

Set `MEMFXXK_API_KEY` in the caller's environment and call the health endpoint. This example uses PowerShell.

```powershell
$headers = @{ Authorization = "Bearer $env:MEMFXXK_API_KEY" }
Invoke-RestMethod http://localhost:8000/v1/healthz -Headers $headers
```

A healthy service and database return `{"status":"ok"}`. Requests require a valid key and otherwise return 401. See `.env.example` for database settings and other runtime options.

## Record and read conversation events {#record-and-read-events}

Reuse the same `session_id` throughout a session. A user message, tool call, and assistant reply can share one `turn_id`. Reuse the `idempotency_key` when retrying an event to get the ID of the previously stored event.

```powershell
$headers = @{
    Authorization = "Bearer $env:MEMFXXK_API_KEY"
    "X-Memfxxk-User" = "demo-user"
    "X-Memfxxk-Project" = "demo-project"
}
$body = @{ events = @(
    @{
        kind = "message"
        role = "user"
        content = "The demo project uses Python 3.12"
        session_id = "demo-session"
        turn_id = "turn-1"
        idempotency_key = "demo-session-turn-1-user"
    }
) } | ConvertTo-Json -Depth 5

$saved = Invoke-RestMethod http://localhost:8000/v1/events -Method Post `
    -Headers $headers -ContentType "application/json" -Body $body
$saved
Invoke-RestMethod "http://localhost:8000/v1/events/$($saved[0].event_id)" -Headers $headers
Invoke-RestMethod "http://localhost:8000/v1/sessions/demo-session/events?from_seq=1&limit=20" `
    -Headers $headers
```

The batch response follows input order and includes each event ID, session sequence number, duplicate flag, and affected memory IDs. Use the same user and project headers to read the events back. The event stores the complete tool output. For long outputs, the derived turn record keeps the beginning and end.

## Run indexing jobs {#run-indexing}

By default the API starts a worker with deterministic fake embeddings for local development. To use an OpenAI-compatible embedding service, set `MEMFXXK_EMBEDDING__PROVIDER=openai_compatible` and configure the endpoint, model, and dimension. For a standalone worker, set `MEMFXXK_WORKER__EMBEDDED=false` in the API process.

```sh
uv run memfxxk worker
uv run memfxxk index ensure
uv run memfxxk index reembed
```

`index ensure` creates the HNSW index for the configured model and dimension and can be run repeatedly. `index reembed` queues jobs for current records and accepts an explicit `--model`. Use a fixed vector dimension per model identifier. The service's `/docs` lists the HTTP endpoints.

## Manage memory directly {#manage-memory}

Set `MEMFXXK_API_KEY`, `MEMFXXK_USER`, and `MEMFXXK_PROJECT` in the shell running the CLI. In Bash, set variables with `export MEMFXXK_USER=demo-user`. Use `--user-level` for personal notes that should follow you across projects.

```sh
uv run memfxxk remember "Run the smoke test before releasing" --important --pin
uv run memfxxk list --important
uv run memfxxk search "release smoke test"
uv run memfxxk show MEMORY_ID --window 1
uv run memfxxk mark MEMORY_ID important --off
uv run memfxxk link FAILURE_ID FIX_ID --purpose fixes
```

`important` affects retrieval ranking. `pinned` adds the memory to the pinned context section. `edit` opens your editor and saves a new version. Use `show --version` to read an earlier version. `forget` deactivates a memory by default. `forget --delete` asks for confirmation before permanently deleting it.

## Documents and context {#documents-and-context}

```sh
uv run memfxxk doc put project.md --content "Release on Tuesdays"
uv run memfxxk doc history project.md
uv run memfxxk context demo-session "How do we release?" --explain
```

The service loads `persona.md` and `project.md` from the applicable scope. It assembles documents, pinned memories, recent turns, and retrieved evidence within separate budgets. `--explain` shows each section's token usage and why items were included. Run `uv run python examples/demo_dev_agent.py` for a demonstration of importance marks, linked failures and fixes, cross-session recovery, and release-policy revisions.

## Connect an agent {#connect-an-agent}

Start the stdio MCP server with `uv run memfxxk mcp` from the repository directory. Pass the same API key, API address, and user, project, and agent scope in the host's environment. The default tools are `memory_search`, `memory_get`, and `memory_remember`. Set `MEMFXXK_MCP_ENABLE_MANAGE=true` to add `memory_mark` and `memory_link`. Python hosts can also assemble context directly through the synchronous or asynchronous SDK.

[Host integration guide and demo](https://github.com/shengming-zhou/memfxxk/blob/main/docs/usage.md)

The repository includes a generic MCP harness. Reuse the session ID to resume a saved conversation. The harness calls context building, compaction, and event tools directly and forwards the allowed search and management tools to the model.

```sh
uv run python -m examples.mcp_harness --session inventory --manage "Find the lesson from the last inventory import"
```

Set `MEMFXXK_DEMO_MODEL_URL`, `MEMFXXK_DEMO_MODEL`, and `MEMFXXK_DEMO_MODEL_API_KEY` when required to connect a model endpoint. Leaving out the model configuration runs the local demo path. Use `--help` to see context and output budget options.

## Team project credentials {#project-credentials}

Administrators can issue credentials bound to a project member. The service fills in scope headers from the credential when they are omitted. Use `--access read` for read access, `--access write` for project collaboration, and `--agent` to bind a specific agent.

Project write credentials can deactivate and reactivate memories. Administrators or authorized hosts can permanently delete memories, source events, or documents.

```sh
uv run memfxxk keys create --org acme --role project --name alice-inventory --project inventory --user alice --access write
uv run memfxxk keys list --org acme
```

To share a memory in the project, drop the user and agent dimensions from its write scope and retain the project dimension. Members can read shared project records and their own private records according to scope. Administrators use `keys revoke KEY_ID --org acme` to revoke access.

## Save and reuse lessons {#procedural-memory}

After a failure and a verified fix, the harness can save the procedure as a lesson. Record two `tool_result` events and set `payload.outcome` to `failure` and `success` respectively. Confirm the scenario, prerequisites, and steps, then save with `POST /v1/lessons` or the Python SDK.

```python
from memfxxk.domain.manage import SaveLessonRequest

saved = client.save_lesson(
    SaveLessonRequest(
        title="Import semicolon-delimited inventory",
        scenario="The import reports a missing quantity column",
        prerequisites=["The file header uses semicolons"],
        steps=["Read the header and select a semicolon delimiter", "Verify the quantity total"],
        result="The quantity column is readable and the total matches the source",
        failure_event_id=failed_event_id,
        verification_event_id=verified_event_id,
        idempotency_key="warehouse-export-v1",
    )
)
```

For a similar task, pass `types: ["lesson"]` to `memory_search` or `POST /v1/search`. Open the full memory and its source references, then check the prerequisites for the current task. To load lessons automatically, include `lesson` in `context_build.search_filters.types` or pin a lesson. With MCP management tools enabled, `memory_save_lesson` accepts the same parameters. With host tools enabled, `event_get` reads the original event.

## Move a Markdown workspace {#markdown-workspace}

Export documents from the current request scope, then switch projects and import them. Export creates a new directory. Import also accepts an existing folder of UTF-8 Markdown files.

```sh
uv run memfxxk --project source workspace export ./workspace-copy
uv run memfxxk --project target workspace import ./workspace-copy
```

The snapshot keeps document text, purpose, and load policy. Repeat `--lesson` with the IDs of lessons you want to move. Selected lessons are saved as Markdown, and `workspace.json` carries their original failure and verification results. Import creates local references so the destination project can read the evidence.

Reimporting identical content skips it. A conflict preserves the destination content. To edit files in a fresh export and write them back to the original project, import with `--update`. The server checks that the exported revision is still current. The snapshot transfers current content. The original workspace keeps revision history and user marks.

## Long sessions and graph search {#long-sessions-and-graph}

The harness decides when to load memory, compact history, and invoke the model. Reserve room for system instructions, tool definitions, the current input, and the response before setting `max_tokens` on `POST /v1/context/build`. If the harness already includes a contiguous suffix of raw messages, set `host_history_from_seq` to its first sequence number.

Compaction has two steps. Send the session, current revision, last closed sequence to cover, and summary limit to `POST /v1/context/compactions`. After checking the draft, send its `draft_id` to `POST /v1/context/compactions/commit`. The summary keeps references to the original event IDs. The repository includes a reference loop at `examples/host_loop.py`.

With a compaction model configured, drafts have `method: llm`. Checkpoints preserve current decisions, open tasks, identifiers, tool results, user preferences and corrections, and unresolved claims. The next compaction processes newly closed turns alongside the previous checkpoint. The bundle may carry recent verbatim user messages marked `retained_user`. Without a configured model, the draft uses `method: extractive_fallback` and preserves excerpts by importance and recency.

`context_build` reports the memory bundle's `pressure` and `pressure_ratio`. By default, occupancy above 70% of the available memory budget returns `warning`. The harness can use this signal to compact an eligible closed prefix early. The ratio covers memory assembled by memfxxk. The harness allocates system instructions, tool definitions, and response headroom. [Compaction implementation](/en/developer-manual/#t16-structured-compaction)

In a 30-question replay of long conversations, DeepSeek Flash compaction used about 620,000 input and 12,500 output tokens per 1,000 turns. Usage varies with message length and compaction frequency. The model summary answered 8 questions correctly, and hybrid retrieval alone answered 7. Model compaction can help carry decisions and open tasks across turns. [Evaluation conditions and results](/en/developer-manual/#compaction-live-evaluation)

Use `PATCH /v1/memories/{id}` to set `priority` from 1 to 5 and add `tags`. Pin short, durable rules and store long references as `on_demand` documents. Saving a revision indexes its Markdown sections and returns heading and revision metadata when those sections match a search. Existing documents remain available by path and gain searchable sections on their next save.

Pass `graph_hops: 1` or `2` to search, or put it in `search_filters` when building context. Search starts with keyword and vector matches and follows links or shared entities on reviewed facts. Inspect `graph_path` to see the chain of evidence.

To reserve room for graph evidence, set `graph_evidence_tokens` on `context_build`. A 1,500-token total budget can reserve up to 300 tokens for graph evidence, for example. Ordinary evidence can use any unused capacity.

Set `evidence_mode` to `excerpt` when the evidence budget is tight. The bundle uses relevant search snippets and still gives you the memory ID and version for reading the full entry.

When background fact extraction is enabled, list pending proposals with `GET /v1/fact-proposals`. Review a proposal at `POST /v1/fact-proposals/{id}/review` using `{"accept": true}` or `{"accept": false}`. For fact searches, `as_of_valid` asks what was effective at a given time and `as_of_recorded` limits what the system could have known by then.

### Use temporal graph memory {#temporal-graph-usage}

Set `MEMFXXK_STRATEGIES__GRAPH=graphiti` to derive entities and relationships from newly closed turns. The five tasks can share a default model or use separate settings for `entity_extract`, `entity_resolve`, `edge_extract`, `edge_resolve`, and `node_summary`. Search with `graph_hops=1` or `2`. Use `as_of_valid` for relationships effective on a given date and `as_of_recorded` to limit what the service could have known. A `graph_fact` can include the relationship text, dates, and original event IDs. Select `MEMFXXK_STRATEGIES__GRAPH=links` for the existing link search. [Temporal graph implementation](/en/developer-manual/#t16-temporal-graph)

The six-question replay showed no answer accuracy gain. Scaled from those conversations, graph writes used about 14.52 million input and 1.49 million output tokens per 1,000 turns. The existing link graph is the default. [Temporal graph evaluation](/en/developer-manual/#temporal-graph-evaluation)

## Configure memory models {#memory-model-configuration}

Compaction and fact extraction can use different models. Set defaults with `MEMFXXK_LLM__DEFAULT__MODEL`, `MEMFXXK_LLM__DEFAULT__BASE_URL`, and `MEMFXXK_LLM__DEFAULT__API_KEY`. Override model names with `MEMFXXK_LLM__TASKS__COMPACT__MODEL` or `MEMFXXK_LLM__TASKS__EXTRACT__MODEL`. Operators can summarize `llm_calls` or set a daily token limit by organization or project. [Model task configuration](/en/developer-manual/#t16-llm-routing)

### Choose how facts are written {#fact-strategy-settings}

- `MEMFXXK_STRATEGIES__FACTS=key_proposal` proposes facts from closed turns for user review. `MEMFXXK_M2__EXTRACT_FACTS=true` also enables this workflow.
- `MEMFXXK_STRATEGIES__FACTS=mem0_classic` extracts facts and decides whether to update, retire, or keep earlier statements. Facts you confirmed, explicitly saved, or marked still go to review. Configure both the `extract` and `reconcile` tasks.
- `MEMFXXK_STRATEGIES__FACTS=mem0_additive` makes one extraction call per turn. New statements and earlier accounts remain available with original sources. Set `MEMFXXK_STRATEGIES__MEM0_ADDITIVE_RANKING=true` to boost matching entities and more recent effective times after retrieval.

Use `off` to pause background extraction. Stored facts remain readable after a strategy change. [Fact write implementation](/en/developer-manual/#t16-fact-strategies)

Scaled from a 51-turn replay, classic used about 3.56 million input and 200,000 output tokens per 1,000 turns. Additive used about 3.41 million input and 500,000 output tokens. The cost varies with message length and the amount of existing memory. [Evaluation results](/en/developer-manual/#mem0-classic-evaluation)

### Add retrieval attributes to notes {#amem-usage}

Set `MEMFXXK_STRATEGIES__NOTES=amem` to generate retrieval descriptions, keywords, and tags for new notes, facts, and lessons, and to add automatic links. Configure `note_enrich`, `note_link`, and `note_evolve` or let them inherit a default model. Use `get_memory` to see `note_attributes`, and `note_history` and `rollback_note` to inspect and restore versions. You control the original text and manually assigned tags.

Both arms answered all four questions in the replay. Scaled from this workload, 1,000 turns of note processing would use about 27.92 million input and 223,000 output tokens. [Implementation and evaluation](/en/developer-manual/#t16-amem)

## Check evidence evaluation {#evidence-evaluation}

Evidence evaluation measures whether a memory backend retrieves the history that supports an answer and tracks query latency. Reproduce it with the frozen LongMemEval_S suite in a dedicated evaluation database. This step requires no chat model. [Evaluation steps in the developer manual](https://shengming-zhou.github.io/memfxxk/en/developer-manual/#evidence-comparison)

QA evaluation uses the same model across no-history, recent-history, and memory-backend groups. [First live model report](https://github.com/shengming-zhou/memfxxk/blob/main/eval/results/m1-smoke/qa-real.md)

The paired evaluation uses the same 30 questions and memory budget to compare hybrid search, a rolling summary, and graph expansion. It records where supporting evidence is lost for each question. [Paired evaluation steps](/en/developer-manual/#m2-paired-qa)

## Model gateway compatibility endpoint {#model-gateway}

For a compatible client, configure `MEMFXXK_GATEWAY__UPSTREAM_BASE_URL` and start the API. Set the client's Chat Completions base URL to memfxxk's `/v1` and use a memfxxk credential. The service uses its separately configured `MEMFXXK_GATEWAY__UPSTREAM_API_KEY` for the model upstream.

The endpoint supports non-streaming text chat with one reply per request. Send `X-Memfxxk-Session` to reuse a conversation and `Idempotency-Key` to deduplicate stored turns. Use the MCP harness when the model needs to search and manage memory itself.
