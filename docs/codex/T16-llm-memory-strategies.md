# T16 · LLM 加工层与各家记忆策略的完整实现

依赖：T00–T15 已合入。参考：`docs/architecture.md` §1、§5、§6、§10，`docs/research/agent-memory-landscape.md`。

## 为什么要做这件事

M1–M3 把存储、作用域、版本、删除传播和接入方式都搭好了。但"各家前沿方法"那一层只做了最外面的一圈：

- 压缩默认是截断。配了模型才会调 LLM，提示词只有 4 行。M2 对照评测里的"摘要组"用的也是截断（`eval/run_m2_paired.py` 传的是空的 `M2Settings()`）。
- 事实提取默认关闭。它只会抽取，不会判断新事实和旧事实的关系，key 字符串一样才算重复。
- 图只认用户手动建的关联，以及字符串完全相同的实体名。没有实体识别、实体去重、关系失效。
- Letta 的自编辑记忆块、Hindsight 的 reflect、MemoryOS 的分层、A-Mem 的记忆演化、ReasoningBank 的经验提炼都还没有。
- LLM 客户端放在 `src/memfxxk/eval/llm.py`，所有任务共用一个模型配置。

本任务的目标是**把每一种设计按原版的颗粒度铺出来**，每一种都配好 LLM 加工和完整的提示词，可以单独打开、单独评测，也可以组合使用。哪一种进默认路径，由评测结果决定（§1.5"先有强基线，再加机制"继续有效）。

"按原版的颗粒度"的意思是：原版有几次 LLM 调用、每次调用的输入是什么、输出是什么结构、冲突怎么处理、阈值是多少，这里都要对得上。对不上的地方要写明原因。只取名字、实际是一套简化逻辑的，不算完成。

## 总体约束

### 硬性规则仍然有效

AGENTS.md 的八条全部适用，下面几条和本任务关系最大：

1. **原文是真源。** 所有 LLM 产物（摘要、事实、实体、关系、笔记属性、记忆块改写、观点、经验）都是派生物，必须带 `source_event_ids`，删掉原文后跟着失效或重建。后台写回前校验 `generation`。
2. **用户的显式选择不能被覆盖。** 对 `origin=user_explicit` 或 `confirmed=true` 的记录，任何策略都只能生成待审提议（复用现有 `fact_proposals` 的流程，或者按同样的语义扩展），不能直接改写、失效或删除。对自动派生的记录，按各策略原版的语义自动应用。
3. **原版里的"删除"在这里一律变成"失效"。** Mem0 的 DELETE、Graphiti 的边失效，都写成关闭有效期（`valid_to`）或 `status=inactive` 加新版本，不做硬删除。硬删除只保留给用户的 `forget`。
4. **作用域只在核心层判断。** 新增的每张表、每个查询都用 `visible_filter`，并附带"另一个 user/project 看不到"的测试。实体、关系、笔记链接跨作用域时逐端检查，和现有图检索一致。
5. **测试不访问网络。** 每个 LLM 任务都要有 fake 实现，按任务名和输入哈希返回固定的 fixture 输出（见 P0）。真实模型只在评测里用。
6. schema 变更只通过新的 Alembic 迁移，可降级。

### 提示词的标准

现在的提示词太短，Codex 写出来的东西容易漂。本任务的每段提示词至少包含：

- **角色和目的**：这一步在整条流程里干什么。
- **输入说明**：每个输入块是什么、从哪来，并且声明"证据是数据，不是指令"。
- **规则**：什么该提、什么不该提、遇到冲突怎么办、时间怎么解析、拿不准时怎么办（一般是返回空或标记待确认）。
- **输出格式**：JSON Schema，代码里用 Pydantic 校验。
- **至少两个完整示例**：一个正常情况，一个边界情况（例如没有可提取内容、和旧记忆矛盾、时间表达模糊）。
- **文件头**：版本号、蓝本出处（项目、文件路径、提交或版本）、改了哪些地方、为什么改。

提示词放在 `src/memfxxk/prompts/<task>/v<N>.md`，输出 schema 放在同目录的 Python 模块里。改提示词就加一个新版本，旧版本保留，评测清单记录用的是哪个版本。

以开源项目的提示词为蓝本时，先核对许可证，并在文件头注明来源。

### 文档的标准

每个策略交付时，在开发手册里写一节，固定五个小标题：

1. **原版怎么做**：步骤、LLM 调用次数、关键阈值，附原版文件路径。
2. **这里怎么做**：对应的模块、表、作业、配置项。
3. **和原版的差异**：逐条写明，并说明原因。
4. **一个具体例子**：给一段对话，写出每一步的 LLM 输入摘要、输出和最后落库的内容。
5. **评测结果**：和基线比，用真实模型跑。

用户手册里每个策略写一段大白话：它解决什么问题、怎么打开、打开后用户能看到什么变化、要花多少钱（每千轮大约多少 token）。

交付说明除了 AGENTS.md 要求的格式，还要加一张表：这次交付里每个策略的"默认开还是关"、"有没有真实模型评测"、"和原版的主要差异"。

## 分阶段交付

一共十个阶段，每个阶段单独交付、单独提交。P0 和 P1 是基础，必须先做。P2 之后的阶段在 P0 完成后可以按下面的顺序推进。每个阶段的验收全部通过再进下一阶段。

```
P0 → P1 → P2 → P3 → P4 → P5 → P6 → P7 → P8 → P9
```

### P0 · LLM 层

把 LLM 客户端从 `eval/` 搬到 `src/memfxxk/llm/`，做成正式的服务组件。

- **按任务路由模型。** 每个任务有自己的配置：模型、地址、密钥、温度、最大输出、超时、重试。没单独配的任务继承默认配置。任务名至少包括：`compact`、`extract`、`reconcile`、`entity_extract`、`entity_resolve`、`edge_extract`、`edge_resolve`、`node_summary`、`note_enrich`、`note_link`、`note_evolve`、`block_edit`、`sleeptime`、`reflect`、`opinion_update`、`trajectory_judge`、`lesson_distill`、`workflow_induce`、`rerank`、`persona_update`。配置形如 `MEMFXXK_LLM__DEFAULT__MODEL`、`MEMFXXK_LLM__TASKS__COMPACT__MODEL`。
- **结构化输出。** 支持服务端 JSON 模式或 JSON Schema，并在客户端做 Pydantic 校验。校验失败时，把错误信息回传给模型重试一次；仍然失败就记为失败，不写入任何派生数据。
- **调用记录。** 新表 `llm_calls`：任务名、提示词版本、模型、输入 token、输出 token、耗时、重试次数、状态、错误码、作用域、关联的作业或请求 ID、输入哈希。默认不存提示词和回复的原文，只有打开调试配置时才保存，保存的内容跟随原文的删除传播。
- **预算。** 可以按作用域设每日 token 上限，超过后，后台任务推迟，同步任务返回明确的错误码。
- **fake 实现。** `FakeLLM` 按 `(task, 输入哈希)` 或按任务名的脚本返回 fixture。测试里禁止真实网络调用（沿用现有约束）。
- **迁移。** `compaction.py`、`facts.py`、`jobs/worker.py` 和评测代码改用新的 LLM 层，原有行为不变。

**验收**：现有测试全绿；每个任务都能单独配模型；校验失败重试一次后放弃，并且没有写入；`llm_calls` 有记录并且作用域隔离；token 上限生效。

### P1 · 对话压缩（蓝本：Codex CLI、MemGPT）

把现在的截断压缩换成真正的 LLM 压缩，保留截断作为显式降级。

原版要点：

- **Codex CLI**（`codex-rs/core/src/compact.rs`）：把完整历史加上压缩提示词发给模型，摘要前面加固定前缀。从新到旧保留最近的用户消息原文，上限约 20,000 token（`COMPACT_USER_MESSAGE_MAX_TOKENS`），超出部分截断。初始指令重新插到最后一条真实用户消息之前。请求超过窗口时，丢掉最老的一条再重试。提示词本身在 `codex_prompts` crate 的 `SUMMARIZATION_PROMPT` 里，**调研时没读到原文，需要 Codex 自己去读并对照**。
- **MemGPT**（论文 §2.1–2.2）：上下文分三块：只读指令、固定大小的工作上下文、FIFO 消息队列。达到警告阈值（例 70%）时插一条"内存压力"系统消息，让模型自己先存重要内容。达到上限时驱逐一部分消息（例 50%），用"旧摘要 + 被驱逐消息"生成新的递归摘要。被驱逐的消息仍可通过检索调回。

本阶段要做的：

- `prepare_compaction` 在配置了 `compact` 模型时走 LLM。输出是结构化的：最新决定、未完成任务、关键标识符（文件、命令、ID）、关键工具结果、用户偏好和更正、待核实事项，每条带来源事件 ID。服务端校验引用的 ID 确实在被压缩的范围里，不在范围里的条目丢弃并记录。
- 按 Codex 的做法保留最近的用户消息原文，额度可配。
- 递归摘要：新摘要以上一版摘要加新增区间为输入，按 MemGPT 的方式生成。
- 在 `context_build` 的返回里加内存压力信号：用量超过可配阈值（默认 70%）时返回 `pressure=warning`，宿主可以据此提示模型或提前压缩。参考 Harness（`examples/mcp_harness.py`、`examples/host_loop.py`）要演示怎么用这个信号。
- 没配模型时仍用截断，但草案上标 `method=extractive_fallback`，评测清单和 bundle 里都能看出来。

**验收**：

- fake LLM 下的流程测试：两次连续压缩、中途重启，引用校验、保留原文、递归摘要都正确。
- 真实模型评测：把 M2 30 题对照里的摘要组改用 LLM 压缩重跑，与截断组、混合组配对比较，报告正确率、证据保留情况和 token 成本。
- 长对话用例（"周五发布"改"周二发布"）在 LLM 压缩下仍然通过。

### P2 · Mem0 风格的事实写入（蓝本：Mem0 论文与 OSS v3）

做成两个可切换的策略。

**`mem0_classic`**（论文 arXiv 2504.19413 §2.1，`mem0/configs/prompts.py` 的 `FACT_RETRIEVAL_PROMPT`、`DEFAULT_UPDATE_MEMORY_PROMPT`）：

1. **提取**：输入是会话摘要、最近 m 条消息（论文取 10）和新的一对消息。输出候选事实列表。
2. **找相似**：对每个候选事实，向量检索 top s 条已有的事实记录（论文取 10），范围是可见、当前、有效的记录。
3. **合并判断**：第二次 LLM 调用。已有记忆用**临时整数 ID** 呈现（避免模型编造 UUID），由代码映射回真实 ID。输出每条的 `event`：ADD / UPDATE / DELETE / NONE，UPDATE 带 `old_memory`。
4. **应用**：ADD 新建 `fact` 记录。UPDATE 写新版本。DELETE 关闭旧事实的有效期。NONE 不动。目标记录是用户确认的或用户显式保存的，改为生成待审提议。

**`mem0_additive`**（OSS v3，`ADDITIVE_EXTRACTION_PROMPT`，`mem0/memory/main.py` 的 `Memory.add()`）：

1. 取最近 10 条消息和 top 10 已有记忆，**一次 LLM 调用**，只新增、不更新。
2. 输出每条带 `attributed_to`（来自用户还是助手）和 `linked_memory_ids`。
3. 内容哈希去重后批量写入，另做实体抽取：先规范化后精确匹配，再按余弦 ≥ 0.95 做语义匹配。
4. 矛盾的事实和旧事实并存，由检索排序决定谁更新：结合语义、BM25 加权和实体加权。实现成检索层的一个可选加权。

两种策略共用现有 `fact` 记录、版本和双时间字段。和现在的 `extract_facts` 的关系：现有逻辑保留为 `key_proposal` 策略，三者通过配置 `MEMFXXK_STRATEGIES__FACTS=mem0_classic|mem0_additive|key_proposal|off` 选择。

**验收**：

- fake LLM 下 ADD/UPDATE/DELETE/NONE 四条路径都有测试，并覆盖：临时 ID 映射、模型返回了不存在的 ID（丢弃）、目标是用户确认记录（转提议）、重复导入幂等、删除原文后事实失效。
- 真实模型：在 LongMemEval knowledge-update 和 temporal-reasoning 题上，和 M1 最强基线配对比较。

### P3 · 时序知识图（蓝本：Graphiti / Zep）

在现有表旁边新建实体图。现有的"显式关联 + 共享实体名"图检索保留，作为 `graph=links` 策略；新的叫 `graph=graphiti`。

原版要点（`graphiti_core/graphiti.py` 的 `add_episode`，`graphiti_core/prompts/` 下各文件，论文 arXiv 2501.13956）：

1. 取最近 n 个 episode 作上下文（论文 n=4）。
2. **实体抽取**（`extract_nodes.py`）：说话人总是第一个实体，排除泛泛的词。输出名称、类型、出现位置。
3. **实体消歧**（`dedupe_nodes.py`）：先用名称向量和全文检索（名称加摘要）找候选，再由 LLM 判断是不是同一个真实对象，返回重复候选的编号，不是就返回 -1。
4. **关系抽取**（`extract_edges.py`）：输出源实体、目标实体、关系类型（大写下划线风格）、事实描述、`valid_at`、`invalid_at`。时间规则：现在时表述的 `valid_at` 取 episode 时间；未来计划的 `valid_at` 取参考时间；软截止日期不算 `invalid_at`；解析不了就留空。
5. **关系去重和矛盾判断**（`dedupe_edges.py` 的 `resolve_edge`）：一次 LLM 调用，只和同一对实体之间的已有边比较，返回重复的和被推翻的边。要有明确的替代或互斥才算推翻，新旧本身不算。被推翻的旧边 `invalid_at` 设成新边的 `valid_at`，并记录系统失效时间。
6. **实体摘要**：用新的边更新实体摘要，2–6 句，第三人称。
7. 社区（标签传播）放到可选的后续步骤，不在本阶段的必做范围。

失效逻辑的具体代码在 `graphiti_core/utils/maintenance/edge_operations.py`，**调研时没读到，需要 Codex 自己去读并对照**。

数据模型（新迁移）：

- `entities`：作用域列、名称、类型、摘要、名称向量、`source_event_ids`、`generation`、创建时间。
- `entity_edges`：作用域列、源实体、目标实体、关系类型、事实文本、事实向量、`valid_at`、`invalid_at`、`created_at`、`expired_at`、`source_event_ids`、`generation`。
- `entity_mentions`（实体出现在哪个 episode，即哪个 turn 记录）。

检索（`search/search_config_recipes.py`）：边和实体都支持 BM25、余弦、BFS 三种召回，以及 RRF、MMR、节点距离、提及次数四种重排（交叉编码器重排放在 P8）。上下文装载时，图证据走现有的 T13 图预算。

**验收**：

- fake LLM 下的流程测试：同一个人用两个称呼时能消歧；"住在北京"之后又说"搬到上海"，旧边失效，`as_of` 查询能分别查到两个时间点的状态；跨作用域的实体不会被合并；删除原文后，只由它支撑的实体和边失效。
- 真实模型：在 LongMemEval multi-session 和 temporal-reasoning 题上，和混合检索、`graph=links` 配对比较，报告证据命中、答题正确率、延时和每千轮写入成本。

### P4 · 自组织笔记网络（蓝本：A-Mem）

策略名 `notes=amem`。原版要点（arXiv 2502.12110 §3，github.com/agiresearch/A-mem）：

1. **笔记构造**：一次 LLM 调用，从内容和时间生成关键词、标签、上下文描述。向量用"内容 + 关键词 + 标签 + 描述"拼接后计算。
2. **建链**：向量取 top k 个近邻作候选，由 LLM 决定和哪些建立链接。向量只做初筛。
3. **记忆演化**：对每个近邻，LLM 看新笔记和其他近邻，决定要不要更新这个近邻的上下文描述、关键词和标签。
4. 检索时取 top k（论文默认 10）。

本阶段要做的：

- 对 `note`、`fact`、`lesson` 记录做笔记构造，属性存在 `metadata` 里，单独建一份增强向量。
- 建链结果写入 `memory_relations`，`purpose=auto_link`，带来源和 LLM 调用 ID。现有的关联展开和图检索会用到它。
- 演化只改**自动生成的属性**（关键词、标签、描述），不改记录正文，也不碰用户确认或用户显式保存的记录的属性。每次演化写一条版本或审计记录，可以回滚。
- 论文附录里演化输出的字段（`should_evolve`、`actions` 等）**是调研时从摘要里看到的，需要 Codex 对照原 PDF 核实**。

**验收**：fake LLM 下建链、演化、回滚、作用域隔离、删除传播都有测试；真实模型在 LoCoMo 或 LongMemEval 上和混合检索比较。

### P5 · 自编辑记忆块与后台整理（蓝本：Letta / MemGPT）

策略名 `blocks=letta`。

原版要点：

- **记忆块**：字段有 `label`、`description`（Agent 靠它判断怎么读写这个块）、`value`、`limit`（字符上限）、`read_only`。渲染进 system prompt 时，每个块包含描述、当前字数和上限，以及内容。同一个块可以挂在多个 Agent 上共享。
- **编辑工具**：Agent 通过工具改自己的块，例如追加、替换、整体重写。**具体的工具名和参数以 `letta-ai/letta` 仓库 `letta/functions/function_sets/base.py` 为准，调研时那个链接是 404，需要 Codex 自己核实**。
- **后台整理**（Letta 文档里叫 dreaming / sleep-time）：后台子 Agent 在完成若干步之后、或发生压缩时触发，回看最近的对话，整理经验，更新记忆。可选再加一轮复查。

本阶段要做的：

- 在现有 `documents` 上加"记忆块"类型（或新表，由 Codex 判断，交付说明写理由）：`label`、`description`、`limit`、`read_only`、`owner`（user 还是 agent）。用户拥有的块默认 `read_only`，Agent 只能提议修改。
- 给模型的 MCP / SDK 工具加记忆块的编辑操作，带 `base_revision` 并发校验，超过 `limit` 时拒绝并返回当前字数。
- `context_build` 按 Letta 的格式渲染记忆块。
- 后台整理作业 `sleeptime_consolidate`：每 N 轮或每次压缩后触发（可配）。读最近的原文和摘要，输出对 Agent 拥有的块的修改，以及对其他记录的提议。每次修改都是一个新修订，带来源。
- 参考 Harness 演示：Agent 在对话里更新自己的块，下一轮装载时能看到。

**验收**：fake LLM 下工具编辑、并发冲突、字数上限、只读保护、后台整理触发和写回校验都有测试；真实模型在长对话用例上看偏好和约定的保持率。

### P6 · 分层记忆与热度（蓝本：MemoryOS）

策略名 `tiers=memoryos`。原版要点（arXiv 2506.06326，github.com/BAI-LAB/MemoryOS 的 `memoryos/` 包）：

- **短期**：对话页（问、答、时间，加一段链式摘要）的 FIFO 队列，容量 7。
- **中期**：按主题分段，每段有 LLM 摘要和关键词集。页从短期移出时，加入得分 F = 余弦(段, 页) + Jaccard(段关键词, 页关键词) 超过 θ=0.6 的段，否则新开一段。
- **热度**：Heat = α·访问次数 + β·段内页数 + γ·exp(−Δt/μ)，α=β=γ=1，μ=1e7 秒。超容量时驱逐热度最低的段。
- **长期**：热度超过 τ=5 的段晋升，由 LLM 更新用户画像、用户知识库和 Agent 特质，然后把段内页数清零。
- **检索**：短期全取；中期先按 F 取 top 5 段，再按语义取 top k 页；长期按相似度取用户知识库和 Agent 特质各 top 10，加完整画像。
- 注意论文里的阈值和仓库 chromadb 示例不一致（示例是 13.0 和 0.7），选定一组并写明来源。

本阶段要做的：段和热度作为派生表实现，页对应现有 turn 记录。晋升写入的画像对应 P5 的记忆块（或文档），走提议流程。在 `context_build` 里作为一种可选的装载策略。

**验收**：热度计算、分段、驱逐、晋升都有确定性测试；真实模型在长对话用例和 LoCoMo 上和默认装载比较。

### P7 · 检索、反思与观点（蓝本：Hindsight）

策略名 `recall=hindsight`、`reflect=hindsight`。原版要点（arXiv 2512.12818，hindsight.vectorize.io 文档）：

- **四类记忆**：世界事实、Agent 自己的经历、带置信度的观点、实体观察摘要。
- **写入**：LLM 把输入改写成 2–5 条自包含的叙述性事实，每条带发生时间段和提及时间、实体、类别。边分四种：实体、时间（权重按 exp(−Δt/σ) 衰减）、语义（余弦超过阈值）、因果。
- **召回**：四路并行：语义、BM25、图（从语义命中点做扩散激活）、时间（解析查询里的时间表达，按距离打分）。RRF（k=60）融合，交叉编码器重排，按 token 预算贪心装入。
- **反思**：最多 10 轮的工具循环，顺序是心智模型、观察、原始事实，引用必须是检索到的 ID。生成回答和候选观点。新事实到来时，对相关观点判断是加强、削弱、矛盾还是无关，置信度相应 ±α（矛盾 −2α），截断到 [0,1]。
- **倾向参数**：怀疑、字面、共情三项，各 1–5 分，外加一段自然语言的使命说明。

本阶段要做的：

- 检索层新增**时间通道**（时间表达解析，中英文都要支持）和**交叉编码器重排**（现有的 `Reranker` 钩子，模型可配；也可以选 LLM 重排，任务名 `rerank`）。这两个对所有策略都可用。
- `POST /v1/reflect` 和对应的 SDK、MCP 工具：按上面的循环实现，返回回答、引用和观点变更。
- 观点作为新的记录类型或 `fact` 的子类型（由 Codex 判断，写进 `docs/decisions/`），带置信度和更新历史。

**验收**：时间通道在"上个月""三月初""last Tuesday"这类表达上有单测；reflect 的引用校验、观点更新、作用域隔离都有测试；真实模型在 LongMemEval temporal-reasoning 和 multi-session 上比较。

### P8 · 经验自动提炼（蓝本：ReasoningBank、Agent Workflow Memory）

在 T09 手动保存经验的基础上加自动提炼。策略名 `lessons=reasoningbank`、`lessons=awm`。

- **ReasoningBank**（arXiv 2509.25140）：LLM 评判只看任务、轨迹和输出，判定成功或失败（温度 0）。每条轨迹最多提炼 3 条经验，字段是标题、一句话描述、内容。成功和失败用不同的提示词：成功写为什么成、什么可以迁移；失败写哪里错、以后怎么避免。失败经验作为防护保留，不丢弃。检索默认 top 1，放进 system prompt，让 Agent 每一步自己决定用不用。新经验直接追加，不做合并。
- **AWM**（arXiv 2409.07429）：从多次经历里归纳反复出现的子流程，把具体值抽象成槽位，存成"描述 + 步骤"。线上模式下，每次任务经过 LLM 评判成功后归纳，并在下一个任务前加入记忆。

本阶段要做的：会话结束或宿主标记任务结束时触发作业。产物写成 `lesson` 记录，`origin=auto_extract`、`confirmed=false`，带轨迹的来源事件。默认作为提议，用户确认后才参与自动装载；可配置为直接可检索但不自动装载。

**验收**：fake LLM 下评判、提炼、提议、确认、删除传播都有测试；真实模型用 T09 的重复工具任务和 AMA-Bench 子集，看第二次成功率和错误复发率。

### P9 · 策略组合与对照评测

- **组合配置**：`MEMFXXK_STRATEGIES__*` 可以同时打开多个策略。每个策略的产物带 `metadata.strategy` 和提示词版本，检索时各自作为通道参与融合，并在 bundle 里写明每条入选内容来自哪个策略。
- **预设组合**：至少提供 `baseline`（现在的混合检索）、`mem0`、`graphiti`、`letta`、`full`（全部打开），作为评测和用户试用的起点。
- **统一评测**：所有策略在同一份冻结题集上跑，用同一个真实答题模型和评委。题集包括 LongMemEval 的各题型、LoCoMo，以及 M2 长对话用例。报告每个策略和每个预设组合的：正确率（按题型）、证据命中、每千轮写入 token、每次查询 token、p50/p95 延时、失败率。结果写到 `eval/results/t16/`，带清单。
- 写一份大白话的结论页，回答"每种策略在哪类问题上有用、值不值它的成本"。默认配置据此调整，调整决定记到 `docs/decisions/`。

## 需要 Codex 自己核对的调研缺口

以下几处是写这份任务书时没读到原文的，实现对应阶段前先去读源码，并在交付说明里写明核对结果：

- Codex CLI 压缩提示词原文（`codex_prompts` crate 的 `SUMMARIZATION_PROMPT`、`SUMMARY_PREFIX`），以及自动压缩的触发阈值。
- Graphiti 的边失效实现（`graphiti_core/utils/maintenance/edge_operations.py`），以及 `RELEVANT_SCHEMA_LIMIT`、`search_config.py` 的默认值。
- Mem0 classic 应用 UPDATE/DELETE 的代码和历史表结构（旧版本的 `mem0/memory/main.py`）。
- Letta 的记忆编辑工具名和参数（`letta/functions/function_sets/base.py`），以及 sleep-time 能改哪些块。
- A-Mem 演化提示词的输出字段（论文附录 B.3）。
- Hindsight 写入和召回模块的源码位置，以及观点更新的 α 取值。

## 不在本任务范围

- KV 缓存或隐状态这类模型内部记忆（M4）。
- 多模态证据。
- 网关的流式支持。
- 改动 `docs/research/`。
