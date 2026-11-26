# T14 · Qdrant 向量适配

依赖 T01、T02。Qdrant 作为可选的余弦向量索引，原文、版本、作用域、文本索引和向量缓存由 PostgreSQL 管理。

- 通过 `MEMFXXK_SEARCH__VECTOR_BACKEND=qdrant` 选择 Qdrant。配置 `MEMFXXK_QDRANT__BASE_URL`、可选的 `MEMFXXK_QDRANT__API_KEY` 和部署专用的 `MEMFXXK_QDRANT__COLLECTION_PREFIX`。
- 先由 PostgreSQL 计算可检索的记录 ID 和代次，再用 Qdrant 的 `has_id` 查候选。候选会进入与 pgvector 相同的融合和最终校验。范围超过 `MEMFXXK_QDRANT__MAX_PREFILTER_IDS`（默认 5000）、镜像索引还未追上或查询失败时，向量通道使用 pgvector。
- 嵌入提交后，同一事务登记 Qdrant 镜像作业。作业按已提交的记录和嵌入发布，仅传输版本行 UUID、代次和向量。硬删除提交时安排镜像移除，记录删除后仍保留待移除的镜像位置，直到 Qdrant 确认。
- 一个 worker 按已配置的 Qdrant 地址处理对应的镜像。迁移地址时，让使用旧地址的 worker 处理旧镜像，独立的集合前缀可用于测试或重建。

```bash
uv run alembic upgrade head
uv run memfxxk index ensure
uv run memfxxk index reembed
uv run memfxxk worker
```

服务恢复后可用 `uv run memfxxk index retry-qdrant` 重新登记已追踪的镜像写入和移除。此命令也适用于搜索已切回 pgvector 的部署。

降级前停掉镜像 worker，按登记在对应 Qdrant 服务删除该部署的专用集合。确认清理后清空 `qdrant_replicas` 并降级。迁移会阻止仍有登记的降级，并在完成降级时清理 `sync_qdrant` 作业。

验收用真实 PostgreSQL 和本地 `httpx.MockTransport` 复现作用域预筛、异步发布、过期响应、故障重试及删除。可先运行 `uv run pytest tests/integration/test_qdrant.py tests/integration/test_embedding_jobs.py tests/integration/test_retrieval.py tests/integration/test_migrations.py`。

本地额外用 Qdrant 1.16.0、PostgreSQL 和测试嵌入跑通了建集合、写入、按作用域检索和硬删除后移除向量。

REST 操作依据 Qdrant 的查询、写入和删除契约。 [Query Points](https://api.qdrant.tech/api-reference/search/query-points) · [Upsert Points](https://api.qdrant.tech/api-reference/points/upsert-points) · [Delete Points](https://api.qdrant.tech/api-reference/points/delete-points)
