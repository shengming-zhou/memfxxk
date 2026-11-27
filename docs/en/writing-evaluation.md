# Memory evaluation for long-form writing

**Fixed allocation remains the default.** In this suite, adaptive allocation found the right chapter but shortened the weapon's name from Ironsilk Blade to Ironsilk, resulting in one fewer correct answer. The planner and feedback changed the weights, while all three adaptive arms loaded identical memory text.

## Setup {#writing-eval-method}

The original Saltglass Harbor suite has 8 documents, 8 setting questions, 5 contradictory drafts, and 3 clean drafts. Five capped arms each get 420 memory tokens, with an uncapped full-text reference. Search uses local BGE English vectors. GPT-5.4 mini answers and checks drafts, while DeepSeek Flash adjusts weights. The feedback arm replays four writing turns that look up omitted lore. The Lorebook arm implements keyword matching, priority order, and whole-entry insertion. [SillyTavern World Info documentation](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)

## Six-arm results {#writing-eval-results}

| Allocation | QA correct / 8 | Complete QA evidence / 8 | Conflicts found / 5 | Correct citations / 5 | Clean false positives / 3 | Mean memory tokens | Common memory prefix | Allocation p50 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Lorebook-style | 8 | 8 | 5 | 0 | 3 | 220 | 73% | 0.4 / 0.8 ms |
| Fixed | 8 | 8 | 5 | 4 | 3 | 310 | 62% | 148 / 186 ms |
| Adaptive | 7 | 6 | 5 | 4 | 3 | 329 | 100% | 121 / 171 ms |
| Adaptive with planner | 7 | 6 | 5 | 4 | 3 | 329 | 100% | 1,143 / 1,676 ms |
| Adaptive with feedback | 7 | 6 | 5 | 4 | 3 | 329 | 100% | 108 / 150 ms |
| Full text | 8 | 8 | 5 | 3 | 3 | 589 | 100% | <1 / <1 ms |

Complete evidence requires the entire annotated setting rule to be loaded. A detected conflict needs a grounded draft excerpt and source quote. The cited file is graded separately. The first prompt labeled drafts `CONFLICT`. All six arms flagged every clean draft. The neutral prompt pilot checked three drafts under fixed and adaptive allocation. Each arm found 2/2 contradictions and left the clean draft unflagged. The six-call pilot leaves the original full conflict results in the table.

In the adaptive arms, three cases found the correct source and lost part of the rule when the allocator chose a summary. `q05` lost the end of the weapon's name. `q07` and `c05` lost the treaty name but kept the word “allies” and the parties. A 100% prefix means adjacent requests loaded identical memory text in this suite. The metric estimates reusable memory text and reflects how little these contexts varied.

The first run used 64 GPT requests and 16 DeepSeek Flash planner calls, totaling 39,084 tokens. Another 32 matching GPT requests reused a local evaluation result. The neutral pilot used 3,112 tokens. End-to-end p50 was 950 ms for fixed and 938 ms for adaptive, with p95 of 1,070 and 1,330 ms. The planner arm reused local answer results, and allocation latency includes the planner call.

## Reproduction files {#writing-eval-files}

[Frozen suite](https://github.com/shengming-zhou/memfxxk/blob/main/eval/suites/t17-saltglass-harbor.json) · [Per-case decisions, usage, and pilot](https://github.com/shengming-zhou/memfxxk/tree/main/eval/results/t17) · [Reproduction steps](/en/developer-manual/#t17-writing-evaluation)
