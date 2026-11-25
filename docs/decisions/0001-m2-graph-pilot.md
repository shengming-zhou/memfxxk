# M2 先试查两跳关联

M3 原计划引入多跳图检索。M2 提前交付一个默认关闭的原型，使用 PostgreSQL 里已有的显式关联，以及用户审核通过的事实所共享的实体。先用关键词和向量找到起点，再向外走一两跳，逐跳检查作用域并返回记忆 ID 路径。这样可以在现有检索上单独测图的增益，再决定值不值得建实体图和图索引。

## 参考的方法

- Microsoft GraphRAG 的局部搜索从实体出发，取相关关系和文本。它的全局搜索用社区摘要回答跨整个语料的问题。这次试验针对有明确起点的多跳证据题。 [Microsoft GraphRAG](https://microsoft.github.io/graphrag/) · [论文](https://www.microsoft.com/en-us/research/publication/from-local-to-global-a-graph-rag-approach-to-query-focused-summarization/)
- HippoRAG 从查询实体出发，用 Personalized PageRank 在知识图上给证据排序。这次试验用固定跳数和候选数，路径容易核查，也能测清楚增加一跳的开销。 [HippoRAG 论文](https://arxiv.org/abs/2405.14831)

## 本地对照

同一批 12 道人工构造的题，每题取前四条。三组共用 PostgreSQL、关键词索引、hash 向量和同一套证据。单跳与两跳题的边是人为标注的。计时每组 36 次，含本地请求路径开销。

| 检索 | 直接证据 | 单跳证据 | 两跳证据 | 合计 | p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 混合 | 4/4 | 0/4 | 0/4 | 4/12 | 35.57 ms |
| 混合加一跳 | 4/4 | 4/4 | 0/4 | 8/12 | 49.45 ms |
| 混合加两跳 | 4/4 | 4/4 | 4/4 | 12/12 | 50.37 ms |

这组题测的是已知关系能否把证据找回来。自动实体的质量、自然语料上的召回、误带入和最终答题准确率要用真实数据另测。原型按请求启用，默认路径继续走混合检索。

```bash
uv run pytest -s tests/integration/test_m2_benchmark.py
```
