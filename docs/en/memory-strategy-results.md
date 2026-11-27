# Memory strategy results

T16 lets the same harness use different memory strategies. Hybrid semantic and keyword retrieval stays available by default. Additional behavior can be enabled for a specific workload.

## Stage results {#stage-results}

| Strategy | Live results | Where to try it |
| --- | --- | --- |
| Structured summaries | The 30-question replay improved from 7/30 to 8/30. | Compress closed history as the working window fills. |
| Mem0 classic / additive | Both variants answered 3/6, compared with 2/6 for hybrid. | Verify write cost when applications see frequent fact changes. |
| Graphiti | 0/6 on graph and temporal questions, compared with 1/6 for hybrid. | Trace a relationship's sources, validity window, or changes. |
| A-Mem | Both groups answered 4/4. Enrichment added write cost. | Try with a large note collection that needs better retrieval labels. |
| Letta blocks | Both groups answered 4/4 preference and commitment questions after 64 turns. | Let a user or agent edit a small set of important preferences and commitments. |
| MemoryOS | 3/4 versus 4/4 on the long dialogue, and 1/8 versus 2/8 on LoCoMo. | Try on recurring topics. Track when recent candidates displace relevant older turns. |
| Hindsight | The six-question check rose from 1/6 to 3/6. Reflect cited at least one gold source on all six. | Test temporal and cross-session questions with room for extra query latency. |
| ReasoningBank / AWM | All three arms completed both repeated imports. AMA QA was 1/3 and 2/3, compared with 2/3 for hybrid. | Consider reviewable lessons for recurring tasks with verifiable tool results. |

The stage checks used different question sets. The unified replay below compares strategies on common inputs. [Stage reproduction commands](/en/developer-manual/#t16-structured-compaction)

## Unified replay {#matrix-results}

The frozen suite covers six LongMemEval question types, LoCoMo, an M2 long dialogue, and one repeated tool task. The focused histories retain annotated evidence and some distractors to check compatibility, source retrieval, and token cost. Every arm uses the same semantic embeddings, answer model, judge, and 1,500-token memory limit. [Frozen suite](https://github.com/shengming-zhou/memfxxk/blob/main/eval/suites/t16-matrix.json)

In the first full-preset trial, A-Mem also enriched the facts extracted by Mem0. One two-turn case had already started 155 neighbor reviews. The interrupted full-preset trial accounted for about 1.23 million known tokens, with two calls still unreported. The final preset lets Mem0 handle factual writes and applies A-Mem to notes and lessons. The other 59 completed cases were reused.

### Final live run {#measured-matrix}

Each of 12 arms completed ten QA cases and one executable repeat import, for 132 checks. Writes used `deepseek-flash`. Answers and grading used `gpt-5.4-mini`. Embeddings came from BGE-small-en-v1.5. Evidence counts original sources loaded in full out of the 13 annotated sources per arm. Query latency includes retrieval and answering. Query tokens include grading. Write tokens per 1,000 turns are scaled from 28 original turns per arm.

| Arm | Correct | Original sources | Write tokens / 1,000 turns | Tokens / query | Query p50 / p95 (s) | Query failure rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 7/11 | 11/13 | 0 | 1,349 | 0.86 / 1.22 | 0% |
| compaction | 9/11 | 10/13 | 722,357 | 1,392 | 0.95 / 1.02 | 0% |
| mem0_classic | 8/11 | 12/13 | 1,947,036 | 1,324 | 0.93 / 1.20 | 0% |
| mem0 | 10/11 | 13/13 | 1,707,321 | 1,299 | 0.96 / 1.17 | 0% |
| graphiti | 6/11 | 11/13 | 5,366,000 | 1,364 | 0.97 / 1.30 | 0% |
| amem | 7/11 | 10/13 | 1,250,857 | 1,365 | 0.92 / 1.21 | 0% |
| letta | 10/11 | 10/13 | 1,469,893 | 1,524 | 0.87 / 1.25 | 0% |
| memoryos | 8/11 | 13/13 | 16,429 | 1,463 | 0.94 / 0.99 | 0% |
| hindsight | 10/11 | 11/13 | 0 | 2,672 | 3.54 / 4.19 | 0% |
| reasoningbank | 7/11 | 11/13 | 90,607 | 1,369 | 0.90 / 1.03 | 0% |
| awm | 7/11 | 11/13 | 87,893 | 1,361 | 0.93 / 1.61 | 0% |
| full | 11/11 | 13/13 | 10,408,464 | 3,491 | 3.80 / 4.51 | 0% |

`full` answered 11/11. It used 291,437 write tokens and averaged 3,491 tokens for each query and its grading. `mem0`, `letta`, and `hindsight` each answered 10/11. Hybrid had at least one original source loaded for each of its four incorrect answers. It also answered one question correctly although its annotated source was excluded or truncated during packing. These focused cases help verify strategy composition and show where evidence was lost.

The formal run used 645,872 write tokens and 219,711 query and grading tokens, or 865,583 total. All 132 checks produced a gradeable result. Six write calls failed on an initial attempt, with no unfinished write jobs. The first full-preset trial is accounted for separately. [Per-type scores and failure rates](https://github.com/shengming-zhou/memfxxk/blob/main/eval/results/t16/p9-matrix/metrics.json) [Dataset, version, and model manifest](https://github.com/shengming-zhou/memfxxk/blob/main/eval/results/t16/p9-matrix/manifest.json)

## Defaults {#defaults}

The default keeps hybrid search, explicit user memory, and host-controlled compaction. Experimental writes, tiers, reflection, and lesson distillation are available through presets or explicit configuration. [Default strategy decision](/decisions/0004-t16-strategy-defaults)
