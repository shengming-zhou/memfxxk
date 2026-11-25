# memfxxk User Manual {#memfxxk}

## Local setup {#local-setup}

Install Python 3.12, uv, and Docker Compose. Run these commands from the repository root. The development database listens on local port 5433.

```sh
docker compose up -d --wait db
uv sync --locked
uv run memfxxk db upgrade
uv run memfxxk keys create --org demo --role host --name local
uv run uvicorn memfxxk.api:create_app --factory
```

The key creation command displays the plaintext key once. Save it in the caller's secret configuration. The service listens on local port 8000 by default. Interactive API documentation is available at `/docs`.

## Check the connection {#check-connection}

Set `MEMFXXK_KEY` in the caller's environment and call the health endpoint. This example uses PowerShell.

```powershell
$headers = @{ Authorization = "Bearer $env:MEMFXXK_KEY" }
Invoke-RestMethod http://localhost:8000/v1/healthz -Headers $headers
```

A healthy service and database return `{"status":"ok"}`. Requests require a valid key and otherwise return 401. See `.env.example` for database settings and other runtime options.

## Record and read conversation events {#record-and-read-events}

Reuse the same `session_id` throughout a session. A user message, tool call, and assistant reply can share one `turn_id`. Reuse the `idempotency_key` when retrying an event to get the ID of the previously stored event.

```powershell
$headers = @{
    Authorization = "Bearer $env:MEMFXXK_KEY"
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

`index ensure` creates the HNSW index for the configured model and dimension and can be run repeatedly. `index reembed` queues jobs for current records and accepts an explicit `--model`. Use a fixed vector dimension per model identifier. The service's `/docs` lists the available event endpoints. Search endpoints are planned for the next milestone.
