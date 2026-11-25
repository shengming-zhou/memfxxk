# T07 · LongMemEval 问答对照组

依赖：T05（工具定义）、T06（数据与入库）。可与 T08 并行。参考：调研报告 §6“五组对照”、`docs/roadmap.md`。

## 目标

在同一批输入上跑 M1 能做的四组对照，得到可复现的答案准确率与成本基线。第五组（提取/画像/图谱等被测机制）留给 M2。

## 范围

### 1. LLM 客户端 `src/memfxxk/eval/llm.py`

- OpenAI 兼容 Chat Completions 客户端（httpx），支持 tool calling；配置 `EVAL__GEN__BASE_URL/MODEL/API_KEY` 与 `EVAL__JUDGE__BASE_URL/MODEL/API_KEY`，生成和 judge 可以是不同模型。
- 固定 `temperature=0`，记录每次调用的输入/输出 token、延时、重试次数。
- 响应缓存：以 `(model, messages, tools, params)` 的哈希为键缓存到 `eval/cache/llm/`（sqlite 或 jsonl 均可），重跑不重复付费；`--no-cache` 可关闭。
- 并发：可配并发度（默认 4），带速率限制重试。

### 2. 对照组

所有组使用同一生成模型、同一提示框架、同一 `question_date` 告知方式（“今天是 <question_date>”），只改变记忆部分：

| 组 | 名称 | 记忆输入 |
| --- | --- | --- |
| G1 | `no_memory` | 只有问题 |
| G2 | `full_history` | 按时间顺序拼接全部 haystack 会话（带日期）。超过 `--max-context-tokens` 的题标记为 `skipped_too_long`，不截断 |
| G3 | `retrieval_inject` | `build_context(query=question, auto_recall=true)` 的 evidence 段（不使用 documents/working），`time_to=question_date`。参数：`k`、`evidence_budget`、`channels`（lexical/vector/hybrid） |
| G4 | `agentic_search` | 不预先注入；模型拿到 `memory_search`、`memory_get` 两个工具（`to_openai_tools()` + `ServiceBackend`，作用域固定为该题的 project，`time_to` 强制为 question_date），最多 `--max-tool-calls`（默认 6）次后必须作答 |

G3 通过 `BuildContextRequest.search_filters`（T04 已提供）传入 `time_to` 与 `channels`，`sections=["evidence"]`。

生成提示：写在 `src/memfxxk/eval/prompts/` 下的文本文件里，版本化（文件名带版本号），清单里记录使用的提示版本。弃答题按 LongMemEval 约定——信息不足时回答无法得知。

### 3. 评分

- 输出与 LongMemEval 官方评测脚本兼容的 `hypotheses.jsonl`（`{"question_id", "hypothesis"}`），并提供 `memfxxk eval longmemeval judge --official` 说明/包装：调用官方仓库的 `evaluate_qa.py`（用户自行 clone 到配置路径，**不要**把官方代码复制进本仓库）。
- 内置 judge：按官方脚本中按问题类型区分的 judge 提示**移植**（以官方仓库当前版本为准，在文件头注明来源 commit），使用配置的 judge 模型。结果中标注 `judge: builtin(<model>)` 与 `judge: official(<model>)`，两者不得混在一张表里比较。
- 指标：总体与按 `question_type` 的准确率；弃答题的正确拒答率单列。

### 4. 成本与过程记录

每题记录：注入 token（G2/G3）、工具调用次数与每次的查询（G4）、生成模型输入/输出 token、总延时、检索 `timings_ms`。汇总为每组的均值与 p50/p95。G4 额外统计“检索结果中包含 answer session 但答错”的比例（区分“没找到”和“找到没用上”）。G3 同样统计。

### 5. 命令与输出

```
memfxxk eval longmemeval qa --dataset s --groups G1,G3,G4 [--limit N] [--k 8] [--evidence-budget 1500] \
    [--channels hybrid] [--max-tool-calls 6] [--max-context-tokens 120000] [--run-name …]
memfxxk eval longmemeval judge <results_dir> [--official]
memfxxk eval report <dir> [<dir> …]
```

输出目录结构沿用 T06：`manifest.json`（含模型、提示版本、全部参数、git commit）、`hypotheses.<group>.jsonl`、`judgements.<group>.jsonl`、`metrics.md`、`per_question.jsonl`、`costs.json`。

## 不做

事实提取、画像、图谱（M2/M3 的第五组）。提示调优——只做一个合理的默认提示，固定下来。

## 验收标准

- 用一个**假 LLM**（测试里注入，按规则返回答案和工具调用）在迷你 fixture 上跑通 G1–G4 全流程，产物文件齐全且格式正确。
- G4：假 LLM 连续请求工具超过上限时，系统强制要求作答并记录 `tool_limit_reached`。
- G4 与 G3 的作用域与 `time_to` 强制生效：测试中断言工具调用无法检索到其他题的 project 或问题日期之后的记忆。
- LLM 缓存命中时不发生网络调用（假 transport 计数验证）。
- `hypotheses.jsonl` 字段名与官方脚本要求一致（以官方仓库为准，在测试里用一行样例校验）。
- 交付说明附上一次真实的小规模运行（`--limit 50`，G1/G3/G4）的 `metrics.md` 与 `costs.json` 摘要；若没有可用的模型 key，说明并附假 LLM 运行结果。
