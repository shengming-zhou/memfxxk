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

## Long sessions and graph search {#long-sessions-and-graph}

The harness decides when to load memory, compact history, and invoke the model. Reserve room for system instructions, tool definitions, the current input, and the response before setting `max_tokens` on `POST /v1/context/build`. If the harness already includes a contiguous suffix of raw messages, set `host_history_from_seq` to its first sequence number.

Compaction has two steps. Send the session, current revision, last closed sequence to cover, and summary limit to `POST /v1/context/compactions`. After checking the draft, send its `draft_id` to `POST /v1/context/compactions/commit`. The summary keeps references to the original event IDs. The repository includes a reference loop at `examples/host_loop.py`.

Use `PATCH /v1/memories/{id}` to set `priority` from 1 to 5 and add `tags`. Pin short, durable rules and store long references as `on_demand` documents. Saving a revision indexes its Markdown sections and returns heading and revision metadata when those sections match a search. Existing documents remain available by path and gain searchable sections on their next save.

Pass `graph_hops: 1` or `2` to search, or put it in `search_filters` when building context. Search starts with keyword and vector matches and follows links or shared entities on reviewed facts. Inspect `graph_path` to see the chain of evidence.

Set `evidence_mode` to `excerpt` when the evidence budget is tight. The bundle uses relevant search snippets and still gives you the memory ID and version for reading the full entry.

When background fact extraction is enabled, list pending proposals with `GET /v1/fact-proposals`. Review a proposal at `POST /v1/fact-proposals/{id}/review` using `{"accept": true}` or `{"accept": false}`. For fact searches, `as_of_valid` asks what was effective at a given time and `as_of_recorded` limits what the system could have known by then.

## Check evidence evaluation {#evidence-evaluation}

Evidence evaluation measures whether a memory backend retrieves the history that supports an answer and tracks query latency. Reproduce it with the frozen LongMemEval_S suite in a dedicated evaluation database. This step requires no chat model. [Evaluation steps in the developer manual](https://shengming-zhou.github.io/memfxxk/en/developer-manual/#evidence-comparison)

QA evaluation uses the same model across no-history, recent-history, and memory-backend groups. [First live model report](https://github.com/shengming-zhou/memfxxk/blob/main/eval/results/m1-smoke/qa-real.md)
