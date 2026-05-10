# Python 重构实施方案

## 技术栈

| 层 | 技术 | 理由 |
|---|---|---|
| Minecraft 协议 | Mineflayer (Node.js) via JSPyBridge | 官方 examples/python/ 已支持，无需重写 |
| 桥接 | TCP Socket (JSON-line 协议) | 自建 Node.js TCP 服务端包裹 Mineflayer + Python async 客户端，替代 JSPyBridge |
| 类型系统 | Pydantic v2 + mypy | 所有数据结构建模，运行时校验 |
| LLM 调用 | litellm / 保留现有 provider adapter | 统一 20+ provider 接口，Python 生态更强 |
| 向量检索 | 文件扫描 → FAISS + rank-bm25（Phase 4b） | 初期零依赖，规模上来后替代手写 cosine 排序，双层混合检索 |
| 记忆层 | 自定义 MemoryLayer + 文件 truth source + 可选 SQLite/Postgres 索引 | 统一管理短期/长期/语义/程序性记忆，避免各模块各自持久化 |
| 图推理 | networkx | 动作状态图 + 知识图谱的内存查询/算法层，底层存储由 MemoryLayer 接管 |
| 任务编排 | 自定义 TaskTree + langgraph（可选） | 树形任务记忆可作为 ProceduralMemory adapter，可选升级为 DAG 编排 |
| 多进程 | asyncio + multiprocessing | 替代 child_process.spawn |
| 包管理 | uv / poetry | 快速依赖解析，lock 文件 |

## 架构总览

```
┌──────────────────────────────────────────────────┐
│  Python 层 (agent 大脑)                           │
│                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ prompter │ │ memory_  │ │ agent_core       │ │
│  │ (LLM)    │ │ (files,  │ │ (Pydantic models │ │
│  │          │ │ later idx│ │  + decision loop)│ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ working  │ │ episodic │ │ semantic memory  │ │
│  │ memory   │ │ memory   │ │ + knowledge graph│ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ procedu- │ │ reflect/ │ │ npc_ctrl + modes │ │
│  │ ral mem  │ │ compact  │ │ + conversation   │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
│                                                  │
│       │ TCP Socket (JSON-line, localhost:9090)   │
│       ▼                                          │
│  ┌──────────────────────────────────────────┐   │
│  │ Mineflayer Socket Server (Node.js)       │   │
│  │ ├── bot 连接管理                          │   │
│  │ ├── pathfinder / collectBlock / pvp      │   │
│  │ ├── 事件系统 (chat, spawn, end...)       │   │
│  │ └── 请求分发 (observe, move, chat...)    │   │
│  └──────────────────────────────────────────┘   │
└──────────────────────────────────────────────────┘
```

## LLM 介入与实时性原则

LLM 适合处理模糊、人类语言、经验归纳和方案取舍；不适合承担事实来源、实时控制和确定性计算。

**核心分工：**

- LLM：理解意图、生成计划草案、总结失败原因、生成候选 memory/skill/task、解释检索结果
- 工具/API：观察世界、读取背包、查询配方、路径规划、执行动作、验证坐标/库存/生命值
- Policy：决定写入、晋升、遗忘、风险分级和是否需要玩家确认

**实时性分层：**

| 层级 | 延迟目标 | 内容 | LLM 介入 |
|---|---:|---|---|
| 前台控制回路 | <100ms | 生命值、怪物接近、岩浆、水、掉落、路径中断 | 不介入；规则/状态机直接处理 |
| 普通动作决策 | 100ms-1s | 下一步移动、采集、合成前校验、skill 匹配 | 尽量不用；优先 tool/skill/KnowledgeGraph |
| 高层规划 | 1-5s | 用户自然语言任务、复杂任务分解、低风险解释 | 可介入，但不得阻塞安全中断 |
| 后台反思/写入 | 秒级到分钟级 | 任务复盘、记忆压缩、candidate skill、索引更新 | 适合介入，后台 worker 执行 |

**后台写入策略：**

游戏前台只记录结构化事件，不等待 LLM 完成长期写入：

```text
AgentLoop / ActionExecutor
  -> 记录 raw event / trace 到队列
  -> 继续游戏前台控制

BackgroundReflectionWorker
  -> 批量读取事件
  -> LLM 总结 memory / skill / task candidate
  -> MemoryPolicy / SkillPolicy 决策
  -> 原子写入文件
  -> 更新 MEMORY.md / SkillIndex / 可选检索索引
```

前台只能依赖已经提交的记忆、skill 和静态知识；后台生成中的候选内容不得影响当前危险处置。任何高风险 skill 修改、长期规则覆盖或删除操作都需要 policy gate，必要时请求玩家确认。

## 分层迁移步骤

### Phase 0: 基础设施搭建 (2-3 天) ✅ 已完成（TCP Socket 方案）

**目标：Python 能控制 Mineflayer，跑通一个 echo bot。**

- [x] 初始化 Python 项目结构 (`pyproject.toml`, `uv.lock`)
- [x] ~~`pip install javascript` 验证 JSPyBridge 可用~~ → 因 JSON 序列化崩溃废弃
- [x] 编写 `socket_server.js`：Node.js TCP 服务端包裹 Mineflayer，JSON-line 协议
- [x] 编写 `socket_bridge.py`：Python async 客户端，请求-响应 + 事件推送
- [x] 跑通 echo bot，确认事件回调和命令执行正常
- [x] 端到端测试 `tests/test_e2e.py` 使用 SocketBridge 替代 JSPyBridge

**最终方案选择：TCP Socket（自建 JSON-line 桥接）**
- 理由：JSPyBridge 的 `json_patch.py` 存在无法修复的 JSON 序列化 bug（Thread-3 崩溃），Kafka/RabbitMQ 对单 bot 控制场景严重过度设计
- 延迟：<1ms（本地回环）
- 序列化完全可控，不存在 `__json__` 兼容性问题

**产出：`src/mc/socket_server.js`, `src/mc/socket_bridge.py`, `src/mc/__init__.py`**

### Phase 1: LLM 模型层迁移 (3-5 天)

**目标：Python 侧接管所有 LLM 调用，Node.js 不再直接调模型。**

**LLM 介入点：**

- 适合：用户自然语言 → 结构化 goal/tool call，失败反馈 → 简短解释，history 压缩和 prompt 构建
- 不适合：发明不存在的命令、绕过 `ToolCallParser`、直接操作 socket 或 Mineflayer API
- 实时性：LLM 调用必须可取消/超时；前台安全中断不等待模型返回

迁移 `models/prompter.js` (~800 行，含 20+ provider)：

- [ ] 用 litellm 统一 provider 接口（OpenAI / Anthropic / Gemini / Ollama / Replicate 等）
- [ ] 实现 `MessageBuilder`：system prompt + examples + conversation + tool definitions 拼接
- [ ] 实现 `ToolCallParser`：解析 LLM 返回的 tool call，映射为 Mineflayer 命令
- [ ] 保留 history 压缩逻辑，用 Python 重写
- [ ] 单元测试：各 provider 的 mock 调用验证

**产出：`src/llm/prompter.py`, `src/llm/message_builder.py`, `src/llm/tool_parser.py`**

### Phase 2: Agent 核心 + 类型建模 (5-7 天)

**目标：Pydantic 建模所有核心概念，agent 决策循环跑通。**

**LLM 介入点：**

- 适合：高层 planning、从 BotState 生成下一步候选动作、解释执行失败并提出重试策略
- 不适合：判断事实真值（背包、坐标、生命值、附近实体）、实时危险处置、路径可达性计算
- 实时性：AgentLoop 前台先执行规则化安全检查，再决定是否调用 LLM；LLM 规划超时则 fallback 到 skill/默认策略/停止

迁移 `agent/agent.js` (~600 行)：

- [ ] Pydantic models：`BotState`, `Inventory`, `Position`, `Entity`, `Action`, `Task`
- [ ] `AgentLoop`：感知 (observe) → 规划 (plan) → 执行 (execute) → 反馈 (feedback) 主循环
- [ ] `ActionExecutor`：将 LLM tool call 转为 Mineflayer API 调用（通过 `mc_bridge`）
- [ ] 迁移 `conversation.js`：对话历史管理，turn 压缩
- [ ] 迁移 `modes.js`：自驱动模式 / 跟随模式 / 采集模式的状态机
- [ ] 集成测试：`"collect 5 oak_log"` 端到端跑通

**产出：`src/agent/core.py`, `src/agent/models.py`, `src/agent/executor.py`, `src/agent/conversation.py`, `src/agent/modes.py`**

### Phase 3: MemoryLayer 契约层 + 写入策略 (2-3 天)

**目标：先统一记忆/技能的读写入口、数据模型和 policy，不提前绑定 SQLite/FAISS，避免后续形成多套互不兼容的存储。**

现代 agent/bot 记忆建议按层分工，而不是把所有内容都塞进一个向量库：

- Working Memory：当前目标、最近观察、短期状态、当前计划，始终可见或高优先级注入 prompt
- Episodic Memory：对话历史、执行轨迹、失败原因、世界事件，按时间和语义检索
- Semantic Memory：Minecraft 配方、物品关系、世界事实、玩家偏好，强调事实一致性
- Procedural Memory：动作图、技能成功率、任务模板、可复用计划，强调“下次怎么做”
- Reflection/Consolidation：后台总结、去重、晋升、遗忘，把低层事件沉淀成规则或模板

Phase 3 只定义"记忆操作系统"的契约：

- 上层只调用 `MemoryService.remember()` / `recall()` / `forget()` / `record_event()`
- 所有写入先经过 `MemoryPolicy`，决定进入 working / episodic / semantic / procedural，或只保留短期
- backend 可替换：Phase 4a 先接文件系统，Phase 3b/4b 再接 SQLite/FAISS/BM25
- skill 通过 procedural adapter 接入 MemoryLayer，但文件上独立存放在 `bots/<name>/skills/`

**LLM 介入点：**

- 适合：把 raw event / execution trace 总结成候选 memory、候选 learned_rule、候选 skill patch
- 不适合：直接拥有最终写入权、覆盖高置信记忆、删除长期记忆、决定高风险 skill 修改
- 实时性：Phase 3 应提供后台写入队列；前台只 `record_event()`，反思、压缩、候选写入由后台 worker 处理

实现内容：

- [ ] 定义 `MemoryRecord` / `MemoryQuery` / `MemoryResult` Pydantic 模型
- [ ] 定义 `MemoryStore` 接口：`put`, `get`, `search`, `update`, `delete`, `compact`
- [ ] 命名空间设计：`bot/<name>/working`, `episodic`, `semantic`, `procedural`
- [ ] 元数据规范：`source`, `confidence`, `created_at`, `updated_at`, `ttl`, `world_id`, `scope`, `tags`, `evidence`, `valid_when`, `invalid_when`
- [ ] 存储抽象：只定义接口，不绑定具体存储；Phase 4a 的 `FileMemoryStore` 是第一个实现
- [ ] 检索抽象：只定义 `search(query) -> list[MemoryResult]`，不提前实现 dense vector / BM25 / RRF
- [ ] 写入策略：哪些事件自动写入、哪些需要 LLM 反思后晋升、哪些只保留短期
- [ ] 压缩策略：conversation summary、execution trace summary、失败原因归因
- [ ] skill 写入边界：自动更新统计字段，步骤/失败模式变更进入候选变更或人工确认
- [ ] 后台反思队列：前台记录事件，后台批量调用 LLM 总结并提交 policy gate

**产出：`src/memory/models.py`, `src/memory/store.py`, `src/memory/service.py`, `src/memory/policies.py`**

### Phase 7a: Static Knowledge 只读版 — minecraft-data 结构化查询 (1-2 天)

**目标：在 MemoryLayer 完整落地前，先用 `minecraft-data` 建立只读的 Minecraft 静态知识查询层，快速降低 LLM 配方、工具链和采集路径幻觉。**

> 开发策略参考：`docs/minecraft-knowledge-strategy.md`。Phase 7a 以 `minecraft-data` 作为 canonical structured data，Minecraft Wiki 只作为后续 supplement/RAG 来源；ActionExecutor 应依赖结构化查询结果，而不是依赖 LLM 读 Wiki 文本后自由发挥。

对应 `roadmap/04-knowledge-graph.md`：

Phase 7a 只处理 Static Knowledge，不写 MemoryLayer：

- Static Knowledge：Minecraft 配方、物品、方块属性、工具等级、生物掉落、食物值、版本规则；可从 `minecraft-data` 重建，默认只读
- 不处理 World Facts：基地坐标、箱子库存、矿点、传送门、危险区域等运行时事实留到 Phase 7b

**LLM 介入点：**

- 适合：把玩家说的自然语言物品名映射到 `minecraft-data` id，例如“附魔台”→ `enchanting_table`
- 不适合：猜配方、猜工具等级、猜掉落物、替代 `minecraft-data` 做事实来源
- 实时性：KnowledgeGraph 查询必须是本地同步/低延迟；LLM 只在名称消歧失败时介入，且不能阻塞危险处置

实现内容：

- [ ] `scripts/export_minecraft_data.js`：从 npm `minecraft-data` 导出指定版本 JSON
- [ ] 导出 `items.json`, `blocks.json`, `recipes.json`, `entities.json`, `foods.json`
- [ ] `KnowledgeGraph` 只读查询：配方、工具需求、掉落物、食物值、简单合成路径
- [ ] 查询接口：`get_recipe()`, `get_tool_requirements()`, `get_missing_materials()`, `can_mine()`, `get_collection_path()`
- [ ] 最小注入策略：按当前任务查询必要 facts/path，不默认注入完整 2-hop 子图
- [ ] 与 ActionExecutor 集成：执行合成/采集前先查询 KnowledgeGraph 校验配方、工具等级和可达路径
- [ ] Wiki supplement 暂缓，等主知识图稳定后再做

**产出：`src/knowledge/knowledge_graph.py`, `scripts/export_minecraft_data.js`, `data/minecraft/<version>/` 示例**

### Phase 3b: MemoryLayer 存储与检索升级路径 (2-3 天，可推迟)

**目标：当文件型记忆稳定后，引入结构化存储、导出和更强检索能力，但不改变 Phase 3 的上层接口。**

> 前置条件：Phase 3 + Phase 4a 已稳定运行，记忆写入 policy 明确，且出现文件扫描变慢、schema migration、并发写入或跨 bot 聚合需求。

**LLM 介入点：**

- 适合：辅助分析写入污染、总结 migration 风险、解释检索失败案例
- 不适合：执行 migration、决定数据冲突真值、替代一致性检查
- 实时性：migration、导出、索引重建全部离线或后台执行；前台使用稳定快照

实现内容：

- [ ] SQLite backend：实现 `MemoryStore` 接口，支持 schema migration、事务写入和 JSON 导出
- [ ] 文件同步策略：文件层仍作为可读 truth source，SQLite 作为结构化索引/查询层
- [ ] 后续可替换为 Postgres/pgvector，但不影响 `MemoryService` 调用方
- [ ] 检索能力扩展点：接入 dense vector、BM25、时间过滤、标签过滤、RRF 融合
- [ ] 索引重建：从 `bots/<name>/memory/**/*.md` 和 `bots/<name>/skills/*.md` 自动重建结构化索引
- [ ] 一致性检查：发现文件和索引冲突时，以文件 frontmatter + body 为准

**产出：`src/memory/sqlite_store.py`, `src/memory/migrations/`, `src/memory/export.py`**

### Phase 4a: 文件型记忆系统 — 参考 Claude Code MEMORY.md 模式 (1-2 天)

**目标：用文件系统实现记忆读写，先跑通再升级，避免过早引入 FAISS 依赖。**

> 写入策略参考：`docs/skill-write-strategy.md` 的 Phase 4 普通记忆写入规则。玩家用短命令表达意图，LLM 生成候选记忆，最终由 MemoryPolicy 决定自动写入、确认或只保留短期。

**LLM 介入点：**

- 适合：玩家短命令 → 结构化记忆草案，补全 Why / How to apply / evidence / valid_when
- 不适合：凭空生成坐标、库存、矿点事实，跳过验证钩子，自动覆盖高置信记忆
- 实时性：文件写入和 `MEMORY.md` 更新走后台队列；前台 recall 只读取已提交索引和详情文件

设计参考 Claude Code 的记忆系统（`~/.claude/projects/<project>/memory/`）：

```
bots/<bot_name>/memory/
├── MEMORY.md              ← 索引文件，始终加载（每行 <150 字符，上限 200 行）
├── places/
│   ├── base.md            ← 命名位置：基地坐标
│   └── diamond_mine.md    ← 命名位置：钻石矿坐标
├── feedback/
│   ├── night_mining.md    ← 教训：夜间采矿死亡
│   └── creeper_combat.md  ← 教训：苦力怕近战策略
├── facts/
│   ├── players.md         ← 玩家偏好、关系
│   └── world_facts.md     ← 世界事实（传送门位置等）
```

> Skill 文件不放在 `memory/skills/`，而是独立存放在 `bots/<bot_name>/skills/`。它们属于 Procedural Memory，但 schema、写入风险和执行语义都不同，避免和普通事实/反馈记忆混写。

记忆文件格式（YAML frontmatter + markdown body）：

```markdown
---
name: base_location
description: 主基地坐标
type: place
created: 2026-05-10T12:00:00Z
updated: 2026-05-10T14:00:00Z
confidence: high          # high / medium / low
ttl: null                 # null = 永不过期
---

基地坐标: (-150, 64, 320)
维度: overworld
标志物: 橡木大屋旁边，有围墙
```

**Why →  How to apply 模式（核心设计）：**
每条记忆必须包含"为什么"和"适用范围"，而不是仅存事实。这让 LLM 能判断边界情况：

```markdown
---
name: avoid_night_caving
type: feedback
---

不要在夜间去洞穴挖矿。

**Why:** 2026-05-08 夜间下洞穴被 3 只骷髅同时射击死亡。
bot 当时只有石剑，没有盾牌和远程武器。

**How to apply:**
- time_of_day=night 且 inventory 中没有 shield 或 bow 时，跳过洞穴相关指令
- 如果 bot 后来拿到盾牌或弓，此规则可能不再适用
- 地面上夜间挖矿（非洞穴）不受此限制
```

实现内容：

- [ ] `MemoryFile` Pydantic 模型：解析 YAML frontmatter + body
- [ ] `MemoryIndex`：读写 `MEMORY.md` 索引，保证不超过 200 行
- [ ] `MemoryStore` 文件实现：`put(record)` → 根据 namespace/type/name 写 .md 文件 + 更新索引
- [ ] `MemoryQuery`：按 type / tag / keyword 过滤，按 confidence / updated_at 排序
- [ ] `MemoryService`：统一 `remember()` / `recall()` / `forget()` 接口
- [ ] 注入策略：索引始终注入 system prompt，详情文件在匹配时按需加载（渐进式披露）
- [ ] 验证钩子：`recall("diamond_mine")` 返回坐标后，bot 先检查坐标是否仍可达再行动
- [ ] 与 Phase 3 MemoryStore 接口对齐，后续可无痛切换到 SQLite/FAISS 后端

**产出：`src/memory/file_store.py`, `src/memory/index.py`, `bots/<name>/memory/` 示例**

**设计决策：**
| 问题 | 选择 | 理由 |
|---|---|---|
| 为什么不用 FAISS 做 v1 | 文件系统先跑通 | 初期记忆量 <200 条，索引全文注入比向量检索更快更准；零依赖 |
| 为什么用 YAML frontmatter | LLM 原生可读 | markdown 文件可直接 grep/edit，调试不需要工具 |
| 为什么强制 Why 字段 | 防止记忆腐烂 | 上下文变了（bot 有了新装备），LLM 能自行判断规则是否还适用 |
| 何时升级到 Phase 4b | 记忆条目 >500 或索引 >200 行 | 文件扫描变慢时引入 FAISS 加速检索，但文件层保留为可读备份 |

---

### Phase 4b: Roadmap P1 — FAISS + BM25 双层检索 (2-3 天，可推迟）

**目标：当记忆规模增长后，用向量检索替换文件扫描。**

> 前置条件：Phase 4a 已稳定运行，记忆条目 >500 或检索延迟 >100ms。
> Phase 4a 的文件层保留作为**可读备份**——FAISS 是加速层而非替代层。

**LLM 介入点：**

- 适合：解释为什么某些检索结果被选中、辅助调试召回质量
- 不适合：参与 RRF 打分、索引重建、schema migration 或检索一致性判断
- 实时性：索引重建和 embedding 生成在后台完成；前台检索使用最后一次可用索引

对应 `roadmap/02-dual-retrieval.md`：

- [ ] FAISS 向量索引：启动时从文件缓存加载，增量更新
- [ ] BM25 稀疏索引 (`rank-bm25`)
- [ ] RRF (Reciprocal Rank Fusion) 融合打分
- [ ] 向量缓存持久化（从 .md 文件自动生成，不额外维护两份数据）
- [ ] 替换文件扫描逻辑，保留 MemoryStore 接口不变
- [ ] 将检索能力注册为 MemoryLayer 的 `search` backend

**产出：`src/retrieval/faiss_index.py`, `src/retrieval/bm25_index.py`, `src/retrieval/fusion.py`**

---

### Phase 5a: 文件型动作图 — 参考 Claude Code skill.md 渐进式披露 (1-2 天)

**目标：用独立 skill 文件存储动作-状态转移，按需加载，避免全图注入 prompt。**

> 写入策略参考：`docs/skill-write-strategy.md`。Phase 5 开发时以该文档为 skill 写入准则：LLM 只生成候选 skill/patch，统计字段可自动更新，执行步骤和失败模式等高风险变更默认需要 policy gate 或玩家确认。

**LLM 介入点：**

- 适合：从成功任务轨迹生成 candidate skill、把失败日志归纳成候选失败模式、解释为什么某个 skill 匹配
- 不适合：无审核修改执行步骤、加入危险动作、绕过前置检查、直接提升 confidence
- 实时性：skill 匹配和高置信执行必须本地快速完成；LLM 生成/修改 skill 在后台进行，当前任务只使用已提交 skill

核心思路来自 Claude Code 的 Skill 系统：每个 skill 是一个独立的 .md 文件，只在用户触发 `/skill-name` 时才加载对应指令到上下文。对 bot 而言：

- 不是把整个动作图序列化进 prompt
- 而是当前状态匹配到哪个 skill，就只加载那个 skill 的 .md 文件
- 类似"工具书"——不需要背下整本书，翻到当前需要的那一页即可

在 MemoryLayer 分层里，skill 属于 Procedural Memory；但 Phase 5a 不把 skill 混入 `bots/<name>/memory/` 目录，而是通过 `SkillStore` / `ProceduralMemoryAdapter` 接入 Phase 3 的统一 policy 和 recall 入口。这样 skill 可以被统一检索、统计和审计，同时保留独立 schema 与更严格的写入门槛。

**Skill 文件格式：**

```markdown
<!-- bots/<name>/skills/mine_diamonds.md -->
---
name: mine_diamonds
description: 寻找并挖掘钻石矿
triggers:
  - situation: "玩家要求挖钻石"
  - state: "inventory 有 iron_pickaxe，pos.y > 0"
requires: [iron_pickaxe_or_better, torches]
confidence: 0.92
success_count: 15
fail_count: 2
last_used: 2026-05-10T15:00:00Z
---

## 前置检查
1. 确认背包有铁镐或更好的镐
2. 确认有至少 16 个火把
3. 确认饱食度 > 10

## 执行步骤
1. !goToSurface() — 先到地面定位
2. !digDown(distance=60) — 挖到 Y=-58
3. !stay(seconds=2) — 等待方块更新
4. 开 2x1 通道，每 8 格放火把
5. 遇到钻石矿 → !collectBlocks(diamond_ore, 1)

## 失败模式
- "No iron_pickaxe in inventory" → 先执行 craft_iron_pickaxe skill
- "Lava detected" → 切换到 block_lava skill
- "Health < 5" → 中断，执行 emergency_escape skill

## 为什么有效
- 钻石在 Y=-58 密度最高（minecraft wiki 验证）
- 2x1 通道防止头顶掉岩浆
- 铁镐是挖掘钻石的最低要求

## 什么时候不适用
- 背包满时先清背包
- 附近有 warden 声波时绝不执行
- peaceful 难度无需钻石装备时跳过
```

**为什么渐进式披露对动作图很重要：**

传统的图查询（embedding → 最近节点 → 最高权重边）有一个问题：即使你只匹配到 1 个节点，图的序列化可能包含邻接节点和边信息，导致 prompt 膨胀。Claude Code 的做法是：匹配到什么就只加载什么，不加载邻接内容。

对 bot 的好处：
1. **上下文精简**：当前是"挖矿"场景，就不会加载"战斗""交易"的 skill 文件
2. **失败链式回退**：skill A 失败 → 自动触发 skill B（如 `mine_diamonds` 失败 → `craft_iron_pickaxe`），形成自然的 sub-skill 调用
3. **人工可编辑**：玩家可以直接编辑 .md 文件优化 bot 行为，不需要重新训练或重建索引
4. **版本控制友好**：git diff 就能看到技能变更

**触发匹配策略（无需 embedding）：**

```python
def match_skill(situation: str, state: BotState, skills_dir: Path) -> Skill | None:
    """按优先级匹配：精确触发词 → 状态条件 → 默认回退"""
    for skill_file in sorted(skills_dir.glob("*.md")):
        skill = parse_skill(skill_file)
        # 1. 触发词匹配
        if any(trigger in situation for trigger in skill.triggers_situation):
            # 2. 状态条件检查
            if all(check_state(cond, state) for cond in skill.triggers_state):
                return skill
    return None
```

初期（<50 个 skill）连 embedding 都不需要，关键词 + 状态条件匹配就足够。

**skill.md 渐进式披露 vs 传统图查询对比：**

| 维度 | skill.md 文件方案（Phase 5a） | networkx 图方案（Phase 5b） |
|---|---|---|
| skill < 50 个 | 关键词匹配 + 状态条件，零依赖，零延迟 | 图构建成本 > 收益 |
| skill 50-100 个 | 仍可用，但触发词歧义开始出现 | 可建立图索引辅助消歧，文件仍是数据源 |
| skill > 100 个 | 需图来管理转移关系、去重、合并相似 skill | 图的价值明确，从文件自动建图 |
| 前置条件是布尔值（有无铁镐） | **优于** embedding——条件检查不会漂移 | — |
| 前置条件是模糊的（"附近比较危险"） | 差于 embedding——无法泛化到新场景 | embedding 相似度更有优势 |
| 上下文膨胀 | **不会**——只加载匹配到的单个 skill 文件 | 可能——图序列化包含邻接节点和边 |
| 调试 | **可解释**——匹配到哪个文件、哪个条件失败一目了然 | 黑盒——需导出/可视化向量才知道为什么匹配 |
| 人工编辑 | 打开 .md 文件直接改 | 需修改图数据或重新导入 |
| 版本控制 | git diff 即可查看技能变更 | 图序列化文件不可读，diff 无意义 |

**关键设计原则：** 文件是 truth source（数据源），图是索引层（加速器）。这和 Claude Code 的 MEMORY.md（索引）+ .md 文件（数据）是同一哲学——索引可以换，数据源永远保留。

实现内容：

- [ ] `SkillFile` Pydantic 模型：解析 YAML frontmatter + 步骤/失败模式
- [ ] `SkillStore` / `ProceduralMemoryAdapter`：接入 Phase 3 的 `procedural` namespace
- [ ] `SkillIndex`：扫描 `skills/` 目录，建立触发词 → 文件映射
- [ ] `SkillMatcher`：给定 situation + BotState → 返回最佳匹配 skill
- [ ] 权重自动更新：每次执行后更新 `success_count / fail_count / confidence`
- [ ] 步骤/失败模式变更策略：默认生成候选变更，高风险 skill 需要人工确认
- [ ] 失败链式回退：skill 执行失败 → 读取 `失败模式` 列表 → 触发对应 sub-skill
- [ ] 与 `ActionExecutor` 集成：高 confidence（>0.9）的 skill 直接执行，低 confidence 走 LLM

**产出：`src/skills/loader.py`, `src/skills/matcher.py`, `bots/<name>/skills/` 示例**

---

### Phase 5b: Roadmap P0 — 图数据库动作图 (2-3 天，可推迟）

**目标：当 skill 文件 >100 个后，用 networkx 图结构管理 skill 间的转移关系。**

> 前置条件：Phase 5a 已稳定运行，skill 文件 >100 个，关键词匹配开始出现歧义。
> Phase 5a 的文件层保留——图数据库是索引层，文件是数据源。

**LLM 介入点：**

- 适合：辅助解释候选 skill 歧义、建议合并相似 skill 的候选方案
- 不适合：替代图查询、直接更新图边权、决定高风险 skill 合并
- 实时性：图构建和相似 skill 合并建议走后台；前台只使用已构建图索引

对应 `roadmap/01-action-state-graph.md`：

- [ ] 从 skill 文件自动构建 `StateNode` 和 `ActionEdge`
- [ ] `StateNode`：从 skill 的 `triggers.state` 字段自动提取
- [ ] `ActionEdge`：从 skill 的 `失败模式` 字段自动构建转移边
- [ ] 图查询：当前状态 → 候选边 → 按 confidence 排序
- [ ] 权重更新：执行后更新 skill 文件的 success/fail 计数
- [ ] 阈值控制：高于 threshold 直接执行，低于走 LLM

**产出：`src/graph/action_graph.py`**

### Phase 6: Roadmap P2 — 任务复盘与 TaskTree Lite (2-3 天)

**目标：把复杂任务的执行过程沉淀成可复盘、可检索、可晋升的任务模板，减少重复规划，但不新增一套强执行系统。**

> 写入策略参考：`docs/skill-write-strategy.md` 的 Phase 6 TaskTree 写入规则。TaskRun/task summary 可自动写，learned_rules 交给 MemoryPolicy，task subtree 晋升 skill 需要多次成功或玩家确认。

**LLM 介入点：**

- 适合：压缩复杂任务日志、生成 task summary、提取 learned_rules、建议可晋升的 subtree
- 不适合：把一次成功直接固化为 skill、直接修改 skill 正文、替代 ActionExecutor 判断成功失败
- 实时性：TaskRun 复盘在任务结束后后台执行；前台只记录结构化 trace，不等待总结完成

对应 `roadmap/03-task-tree.md`：

Phase 6 的定位：

- Phase 5a skill = 单个稳定、可复用、可直接执行的动作模板
- Phase 6 task tree = 一次复杂任务的分解、执行记录和复盘结果
- TaskTree 默认只作为 planning hint 注入 LLM，不直接自动执行
- 多次成功、结构稳定的 task subtree，可通过 policy 晋升为 Phase 5a skill
- 失败复盘中的稳定教训写入 `memory/feedback/`，而不是直接修改 skill 步骤

实现内容：

- [ ] `TaskNode` 数据结构：goal + subtasks + dependencies + executions + learned_rules + outcome
- [ ] `TaskRun` 执行记录：输入目标、使用过的 memory/skill、关键状态变化、成功/失败原因
- [ ] 轻量检索：按 goal keywords、tags、outcome、updated_at 过滤排序；embedding 检索推迟到 Phase 3b/4b
- [ ] 模板注入：只注入匹配任务的高层分解、前置条件和 learned_rules，不注入完整执行日志
- [ ] 执行后复盘：把 raw trace 压缩成 task summary，并交给 MemoryPolicy 判断是否写入 feedback/facts/procedural
- [ ] 晋升策略：成功次数、失败率、上下文稳定性达到阈值后，生成 candidate skill，默认需要人工确认
- [ ] 任务树作为 procedural memory adapter 接入 MemoryLayer，同时保留 `bots/<name>/tasks/` JSON 导出便于调试

**产出：`src/task/task_tree.py`**

### Phase 7b: World Facts / Semantic Memory 集成 (1-2 天)

**目标：在 Phase 4a 文件型记忆和 Phase 7a 静态知识稳定后，把运行时发现的世界事实写入 Semantic Memory，并与 Static Knowledge 组合使用。**

> 开发策略参考：`docs/minecraft-knowledge-strategy.md`。Phase 7b 只处理 World Facts：bot 在具体世界里观察到的事实必须经过 MemoryLayer policy 写入，并带 `world_id`, `ttl`, `confidence`, `evidence`。

**LLM 介入点：**

- 适合：给玩家标记的地点/箱子生成可读名称、把观察事实整理成候选 world fact、解释为什么使用某个库存来源
- 不适合：凭空断定箱子内容、矿点坐标或危险区域，替代 bot 观察验证
- 实时性：世界事实写入走后台；前台规划可读取已提交 facts，若事实缺失则重新观察或执行保守策略

对应 `roadmap/04-knowledge-graph.md`：

Phase 7b 只处理 World Facts，不重复实现 Phase 7a 的静态知识：

- World Facts：基地坐标、矿点、箱子库存、传送门、危险区域；由 bot 运行时发现，写入 `bots/<name>/memory/facts/`
- Static Knowledge 仍由 Phase 7a 的 `KnowledgeGraph` 只读查询提供

实现内容：

- [ ] `WorldFactService`：将 `LOCATED_AT` / `STORED_IN` / `DANGEROUS_NEAR` 等运行时事实交给 MemoryLayer 写入
- [ ] 世界事实必须标记 `source`, `confidence`, `world_id`, `evidence`, `ttl`, `valid_when`, `invalid_when`
- [ ] 查询组合：当前任务先查 Phase 7a Static Knowledge，再查 World Facts 补充具体坐标、库存和风险
- [ ] 与 ActionExecutor 集成：执行合成/采集前结合 KnowledgeGraph 和 World Facts 选择最少采集路径
- [ ] 静态知识和世界事实冲突时，静态知识优先；世界事实过期或验证失败时降 confidence / forget
- [ ] Wiki supplement 和 `minecraft-data` 冲突时，不覆盖主数据，进入 review

**产出：`src/knowledge/world_facts.py`, `bots/<name>/memory/facts/` 示例**

### Phase 8: Deferred — 多 bot / NPC 控制迁移 (按需)

**目标：当确实需要多 bot 协作、NPC 生命周期管理或跨实例状态同步时，再迁移多 bot 管理逻辑。当前单 bot 主线不依赖此阶段。**

> 暂不进入主线。推迟可以避免过早引入多实例生命周期、状态同步、命名空间隔离、并发写入和 socket 多连接复杂度。

**LLM 介入点：**

- 适合：未来做高层任务分配，例如“bot A 采木头，bot B 建墙”
- 不适合：生命周期管理、socket 多实例调度、锁、并发写入冲突解决
- 实时性：多 bot 协作中的 LLM 只做高层调度，单 bot 的安全中断和状态同步仍由规则/控制层处理

未来触发条件：

- 需要同时运行多个 bot
- 需要 bot 之间分工协作或互相通信
- 需要统一管理多个 Mineflayer 连接生命周期
- 单 bot MemoryLayer / AgentLoop 已稳定，且并发写入策略已验证

未来实现内容：

- [ ] `NPCManager`：管理多个 bot 实例的生命周期
- [ ] `NPCController`：指令转发、状态同步、协作任务分配
- [ ] 与 `mc_bridge` 的多实例适配
- [ ] MemoryLayer 命名空间隔离：`bot/<name>/...` + shared world facts
- [ ] 多 bot 并发写入与冲突解决

**产出：`src/npc/manager.py`, `src/npc/controller.py`（Deferred）**

### Phase 9: 集成测试与性能验证 (3-5 天)

- [ ] 端到端回归测试：采集、合成、建造、战斗
- [ ] MemoryLayer 回归测试：写入、检索、压缩、命名空间隔离、schema migration
- [ ] TaskTree 回归测试：任务复盘、模板检索、candidate skill 晋升
- [ ] KnowledgeGraph 回归测试：配方查询、工具链校验、采集路径查询
- [ ] World Facts 回归测试：世界事实写入/过期、库存事实复用、静态知识冲突处理
- [ ] 多 bot 并发测试（Deferred，Phase 8 启用后再做）
- [ ] 与现有 Node.js 版本的 A/B 对比（延迟、token 消耗、成功率）
- [ ] 文档：迁移指南、开发环境搭建

## 风险矩阵

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| ~~JSPyBridge 高频事件性能瓶颈~~ | ~~中~~ | ~~高~~ | **已解决** — 改用 TCP Socket 桥接，JSON-line 协议 |
| ~~JSPyBridge 跨语言调试困难~~ | ~~高~~ | ~~中~~ | **已解决** — Socket 协议可独立抓包调试 |
| litellm 覆盖不全某些 provider | 低 | 中 | 保留自定义 adapter 扩展能力 |
| 记忆层过早复杂化 | 中 | 中 | Phase 3 只实现稳定接口 + policy；SQLite/FAISS/RRF 放到 Phase 3b/4b，按规模引入 |
| 多套记忆重复写入导致上下文污染 | 中 | 高 | 统一通过 MemoryLayer policy 写入，记录 source/confidence/tags/ttl，定期 compact |
| 图数据内存膨胀 | 低 | 低 | 设置节点/边上限，LRU 淘汰，底层持久化交给 MemoryLayer |
| 双版本维护负担（过渡期） | 高 | 中 | 以 feature flag 控制切换，逐步下线 Node.js agent 逻辑 |

## 时间线

```
Week 1:  Phase 0 (基础设施) ✅ + Phase 1 (LLM 层) ✅ + Phase 2 (Agent 核心) ✅
Week 2:  Phase 3 (MemoryLayer 契约 + policy) + Phase 7a (Static Knowledge 只读版)
Week 3:  Phase 4a (文件型记忆 MEMORY.md) + Phase 5a (文件型动作图 skill.md)
Week 4:  Phase 6 (TaskTree Lite) + Phase 7b (World Facts / Semantic Memory) + 集成加固
Week 5:  Phase 3b (SQLite/结构化索引，按需) + Phase 4b (FAISS 检索，按需) + Phase 5b (图数据库，按需)
Week 6:  Phase 9 (集成测试与性能验证)
Deferred: Phase 8 (多 bot / NPC 控制迁移，按需)
```

**总计：约 6 周。Phase 3 先收敛接口和 policy，Phase 7a 提前提供高性价比的静态知识校验，Phase 4a/5a 用文件系统跑通记忆和 skill，Phase 6/7b 聚焦单 bot 的任务复盘和世界事实组合，降低过早抽象的返工风险。3b/4b/5b/8 推迟到规模真正需要时引入。**

## 目录结构（目标）

```
mindcraft-py/
├── pyproject.toml
├── uv.lock
├── src/
│   ├── mc/
│   │   ├── socket_server.js   # Node.js TCP 服务端 (Mineflayer 包裹)
│   │   ├── socket_bridge.py   # Python async TCP 客户端
│   │   ├── bridge.py          # [已废弃] JSPyBridge 封装
│   │   └── events.py          # [已废弃] JSPyBridge 事件注册
│   ├── llm/
│   │   ├── prompter.py        # LLM 调用统一接口
│   │   ├── message_builder.py # Prompt 拼接
│   │   └── tool_parser.py     # Tool call 解析
│   ├── agent/
│   │   ├── core.py            # Agent 主循环
│   │   ├── models.py          # Pydantic 数据模型
│   │   ├── executor.py        # 动作执行器
│   │   ├── conversation.py    # 对话管理
│   │   └── modes.py           # 行为模式状态机
│   ├── memory/
│   │   ├── models.py          # MemoryRecord / MemoryQuery / MemoryResult
│   │   ├── store.py           # MemoryStore 接口
│   │   ├── file_store.py      # Phase 4a: 文件系统实现（MEMORY.md 索引 + .md 记忆文件）
│   │   ├── index.py           # Phase 4a: MEMORY.md 索引读写
│   │   ├── sqlite_store.py    # Phase 3b: SQLite 结构化索引/查询层（可推迟）
│   │   ├── service.py         # 统一读写、检索、压缩入口
│   │   └── policies.py        # 写入、晋升、遗忘、注入策略
│   ├── skills/
│   │   ├── loader.py          # Phase 5a: Skill 文件解析（YAML frontmatter + markdown）
│   │   ├── store.py           # Phase 5a: ProceduralMemoryAdapter / SkillStore
│   │   └── matcher.py         # Phase 5a: 触发词 + 状态条件匹配器
│   ├── retrieval/
│   │   ├── faiss_index.py     # Phase 4b: 稠密向量索引（可推迟）
│   │   ├── bm25_index.py      # Phase 4b: 稀疏关键词索引（可推迟）
│   │   └── fusion.py          # Phase 4b: RRF 融合（可推迟）
│   ├── graph/
│   │   └── action_graph.py    # Phase 5b: 动作-状态图（从 skill 文件自动构建，可推迟）
│   ├── knowledge/
│   │   ├── knowledge_graph.py # Phase 7a: Minecraft 静态知识查询
│   │   └── world_facts.py     # Phase 7b: 运行时世界事实写入 MemoryLayer
│   ├── task/
│   │   └── task_tree.py       # Phase 6: TaskTree Lite / 任务复盘
│   └── npc/
│       ├── manager.py         # Deferred: 多 bot 管理
│       └── controller.py      # Deferred: 指令转发
├── tests/
│   ├── test_llm/
│   ├── test_agent/
│   ├── test_memory/
│   ├── test_retrieval/
│   └── test_integration/
├── bots/<name>/               # 每个 bot 的数据持久化
│   ├── memory/
│   │   ├── MEMORY.md           # 记忆索引
│   │   ├── places/             # 命名位置
│   │   ├── feedback/           # 经验教训
│   │   └── facts/              # 世界事实
│   ├── skills/
│   │   ├── mine_diamonds.md    # 技能模板
│   │   └── build_shelter.md
│   └── tasks/
│       └── build_base_run.json # Phase 6: 任务复盘导出
├── data/
│   └── minecraft/<version>/    # Phase 7a: minecraft-data 导出的结构化知识
│       ├── items.json
│       ├── blocks.json
│       ├── recipes.json
│       ├── entities.json
│       └── foods.json
├── scripts/
│   └── export_minecraft_data.js # Phase 7a: 导出 minecraft-data JSON
├── node_modules/              # Node.js 依赖（socket_server.js）
│   ├── skills.js              # 动作执行（退化版）
│   └── world.js               # 世界状态查询
```
