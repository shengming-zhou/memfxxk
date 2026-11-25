# T06 · LongMemEval 检索评测

依赖：T02。可与 T03/T04 并行。参考：`docs/roadmap.md`“M1 完成定义”第 4 条、调研报告 §6。

## 目标

不调用 LLM，先量化检索本身：在 LongMemEval 上比较 lexical / vector / hybrid 的证据召回，按问题类型分列，结果带完整配置清单，可复现。

## 范围

### 1. 数据 `src/memfxxk/eval/longmemeval/`

- `fetch` 命令：从官方发布位置下载 `longmemeval_s`、`longmemeval_m`、`longmemeval_oracle` 到 `eval/data/longmemeval/`（不提交到 git）。**先读官方仓库 https://github.com/xiaowu0162/LongMemEval 的 README 确认当前下载地址与文件名**（README 指向 Hugging Face 上的数据集，有清洗版），记录下载文件的 sha256 到清单。
- 加载器：解析每道题的 `question_id, question_type, question, answer, question_date, haystack_session_ids, haystack_dates, haystack_sessions, answer_session_ids`，以及 turn 上的 `has_answer` 标注。字段名以实际数据为准，写解析测试（用仓库里手写的 2 道题的迷你 fixture，不依赖下载）。
- 识别弃答题（`question_id` 以 `_abs` 结尾）：检索指标中排除，单独计数。

### 2. 入库

- 每道题一个隔离作用域：`org=eval`，`project_id=<question_id>`，`user_id=eval-user`。
- 每个 haystack session → `session_id=<question_id>:<haystack_session_id>`；`event_time` 解析自 `haystack_dates`（格式形如 `2023/05/20 (Sat) 02:21`，解析器要有单测）。
- 轮次：相邻的 user + assistant 消息共享一个 `turn_id`（`<session_id>:<idx>`）；记录 turn 记录与原始 `(session_id, turn_idx, has_answer)` 的映射，用于 turn 级指标。
- 直接用进程内 `MemoryService`（不走 HTTP），批量 ingest，最后 `drain()`。
- 入库是可续跑的：以 `(dataset, question_id)` 记录完成状态，重跑时跳过已完成题（数据库里有就不重写）；提供 `--reset` 清空 `org=eval`。
- 嵌入走 `CachedEmbedder`：S 子集的会话大量跨题重复，缓存是成本关键。入库结束打印缓存命中率。

### 3. 指标

对每道非弃答题，以 `question` 为查询、`time_to=question_date`（只用问题之前的记忆），按 `channels` 跑 search，k 取 `{5, 10, 20}`：

- **session 级**：`recall_any@k`（top k 中至少命中一个 answer session）、`recall_all@k`（覆盖全部 answer session）。
- **turn 级**：`recall_any@k`、`recall_all@k`（以 `has_answer=true` 的轮为目标）。
- `ndcg@k`（session 级，二值相关性）。
- 按 `question_type` 分组和总体各给一份。

运行配置（命令行参数，写入清单）：`dataset, limit, question_types, channels（lexical | vector | hybrid | hybrid+important 不适用可省略）, k_list, embedding model/dim, tokenizer 版本, search 参数（N、RRF k、权重、ef_search）`。

### 4. 输出

`eval/results/<UTC时间戳>-<run_name>/`：
- `manifest.json`：上述全部配置、git commit、数据文件 sha256、库中记录数、运行耗时。
- `metrics.json` 与 `metrics.md`（按类型的表格）。
- `per_question.jsonl`：每题每个 channel 的命中情况、top k 的 memory_id 与得分明细，方便误差分析。
- `latency.json`：search 的 `timings_ms` 分位数（p50/p95/p99）。

命令：
```
memfxxk eval longmemeval fetch
memfxxk eval longmemeval ingest --dataset s [--limit N]
memfxxk eval longmemeval retrieval --dataset s --channels lexical,vector,hybrid --k 5,10,20 [--limit N] [--run-name …]
memfxxk eval report <results_dir> [<results_dir> …]   # 多次运行并排对比的 markdown 表
```

## 不做

LLM 问答与 judge（T07）。分词或检索参数调优——本任务只产出基线和工具；调参作为后续独立实验。

## 验收标准

- 迷你 fixture（手写 2–3 道题，含一道弃答题、一道多 answer session 题）上的端到端测试：入库 → 检索 → 指标数值与手算一致。
- 日期解析、turn 配对（含末尾只有 user 没有 assistant 的情况）有单测。
- `time_to=question_date` 生效：fixture 中放一个问题日期之后的“答案”会话，断言不被召回。
- 续跑：中断后重跑不重复入库（断言事件数不变）。
- 用 fake embedder 在 `longmemeval_oracle` 或 `--limit 20` 的 S 上能完整跑通（手动运行，不进默认测试）；在交付说明里附上一份真实运行的 `metrics.md`（若本地没有真实嵌入服务，附 fake 结果并注明）。
