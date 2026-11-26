"""Serve a CPU embedding model for local evals.

Run with uv run --locked --with fastembed==0.7.4 python eval/serve_embeddings.py.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastembed import TextEmbedding

MODEL = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384


class EmbeddingRequest(BaseModel):
    model: Literal["BAAI/bge-small-en-v1.5"]
    input: str | list[str] = Field(min_length=1)
    encoding_format: Literal["float"] = "float"
    dimensions: Literal[384] = DIMENSIONS


def create_app(embedder: TextEmbedding) -> FastAPI:
    app = FastAPI(title="memfxxk local evaluation embeddings")

    @app.post("/v1/embeddings")
    def embeddings(request: EmbeddingRequest) -> dict[str, object]:
        texts = [request.input] if isinstance(request.input, str) else request.input
        return {
            "object": "list",
            "model": MODEL,
            "data": [
                {"object": "embedding", "index": index, "embedding": vector.tolist()}
                for index, vector in enumerate(embedder.embed(texts))
            ],
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--threads", type=int, default=min(os.cpu_count() or 1, 4))
    parser.add_argument("--cache-dir", type=Path, default=Path(".uv-cache/fastembed-models"))
    parser.add_argument("--offline", action="store_true", help="Use cached model weights only")
    parser.add_argument(
        "--execution-provider",
        choices=("CPUExecutionProvider", "DmlExecutionProvider"),
        default="CPUExecutionProvider",
    )
    args = parser.parse_args()

    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from fastembed import TextEmbedding

    model = TextEmbedding(
        model_name=MODEL,
        cache_dir=str(args.cache_dir),
        threads=args.threads,
        providers=list(dict.fromkeys([args.execution_provider, "CPUExecutionProvider"])),
        local_files_only=args.offline,
    )
    uvicorn.run(
        create_app(model),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
