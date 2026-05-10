# Python 重构实施方案

## 技术栈

| 层 | 技术 | 理由 |
|---|---|---|
| Minecraft 协议 | Mineflayer (Node.js) via JSPyBridge | 官方 examples/python/ 已支持，无需重写 |
| 桥接 | TCP Socket (JSON-line 协议) | 自建 Node.js TCP 服务端包裹 Mineflayer + Python async 客户端，替代 JSPyBridge |
| 类型系统 | Pydantic v2 + mypy | 所有数据结构建模，运行时校验 |
| LLM 调用 | litellm / 保留现有 provider adapter | 统一 20+ provider 接口，Python 生态更强 |
| 向量检索 | FAISS + rank-bm25 | 替代手写 cosine 排序，双层混合检索 |
| 记忆层 | 自定义 MemoryLayer + SQLite/Postgres 抽象 | 统一管理短期/长期/语义/程序性记忆，避免各模块各自持久化 |
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
│  │ (LLM)    │ │ (FAISS+  │ │ (Pydantic models │ │
│  │          │ │ BM25+KG) │ │  + decision loop)│ │
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

迁移 `models/prompter.js` (~800 行，含 20+ provider)：

- [ ] 用 litellm 统一 provider 接口（OpenAI / Anthropic / Gemini / Ollama / Replicate 等）
- [ ] 实现 `MessageBuilder`：system prompt + examples + conversation + tool definitions 拼接
- [ ] 实现 `ToolCallParser`：解析 LLM 返回的 tool call，映射为 Mineflayer 命令
- [ ] 保留 history 压缩逻辑，用 Python 重写
- [ ] 单元测试：各 provider 的 mock 调用验证

**产出：`src/llm/prompter.py`, `src/llm/message_builder.py`, `src/llm/tool_parser.py`**

### Phase 2: Agent 核心 + 类型建模 (5-7 天)

**目标：Pydantic 建模所有核心概念，agent 决策循环跑通。**

迁移 `agent/agent.js` (~600 行)：

- [ ] Pydantic models：`BotState`, `Inventory`, `Position`, `Entity`, `Action`, `Task`
- [ ] `AgentLoop`：感知 (observe) → 规划 (plan) → 执行 (execute) → 反馈 (feedback) 主循环
- [ ] `ActionExecutor`：将 LLM tool call 转为 Mineflayer API 调用（通过 `mc_bridge`）
- [ ] 迁移 `conversation.js`：对话历史管理，turn 压缩
- [ ] 迁移 `modes.js`：自驱动模式 / 跟随模式 / 采集模式的状态机
- [ ] 集成测试：`"collect 5 oak_log"` 端到端跑通

**产出：`src/agent/core.py`, `src/agent/models.py`, `src/agent/executor.py`, `src/agent/conversation.py`, `src/agent/modes.py`**

### Phase 3: MemoryLayer 统一记忆底座 (3-5 天)

**目标：先统一记忆读写接口，再迁移 P0/P1/P2/P3，避免后续形成多套互不兼容的存储。**

现代 agent/bot 记忆建议按层分工，而不是把所有内容都塞进一个向量库：

- Working Memory：当前目标、最近观察、短期状态、当前计划，始终可见或高优先级注入 prompt
- Episodic Memory：对话历史、执行轨迹、失败原因、世界事件，按时间和语义检索
- Semantic Memory：Minecraft 配方、物品关系、世界事实、玩家偏好，强调事实一致性
- Procedural Memory：动作图、技能成功率、任务模板、可复用计划，强调“下次怎么做”
- Reflection/Consolidation：后台总结、去重、晋升、遗忘，把低层事件沉淀成规则或模板

实现内容：

- [ ] 定义 `MemoryRecord` / `MemoryQuery` / `MemoryResult` Pydantic 模型
- [ ] 定义 `MemoryStore` 接口：`put`, `get`, `search`, `update`, `delete`, `compact`
- [ ] 命名空间设计：`bot/<name>/working`, `episodic`, `semantic`, `procedural`
- [ ] 元数据规范：`source`, `confidence`, `created_at`, `updated_at`, `ttl`, `world_id`, `scope`, `tags`
- [ ] 存储抽象：本地优先 SQLite + JSON 导出；后续可替换 Postgres/pgvector
- [ ] 检索抽象：统一封装 dense vector、BM25、时间过滤、标签过滤、RRF 融合
- [ ] 写入策略：哪些事件自动写入、哪些需要 LLM 反思后晋升、哪些只保留短期
- [ ] 压缩策略：conversation summary、execution trace summary、失败原因归因

**产出：`src/memory/models.py`, `src/memory/store.py`, `src/memory/service.py`, `src/memory/policies.py`**

### Phase 4a: 文件型记忆系统 — 参考 Claude Code MEMORY.md 模式 (1-2 天)

**目标：用文件系统实现记忆读写，先跑通再升级，避免过早引入 FAISS 依赖。**

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
└── skills/
    ├── mine_diamonds.md   ← 技能模板（见 Phase 5a）
    └── build_shelter.md
```

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
- [ ] `MemoryStore` 文件实现：`put(path, memory)` → 写 .md 文件 + 更新索引
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

核心思路来自 Claude Code 的 Skill 系统：每个 skill 是一个独立的 .md 文件，只在用户触发 `/skill-name` 时才加载对应指令到上下文。对 bot 而言：

- 不是把整个动作图序列化进 prompt
- 而是当前状态匹配到哪个 skill，就只加载那个 skill 的 .md 文件
- 类似"工具书"——不需要背下整本书，翻到当前需要的那一页即可

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
- [ ] `SkillIndex`：扫描 `skills/` 目录，建立触发词 → 文件映射
- [ ] `SkillMatcher`：给定 situation + BotState → 返回最佳匹配 skill
- [ ] 权重自动更新：每次执行后更新 `success_count / fail_count / confidence`
- [ ] 失败链式回退：skill 执行失败 → 读取 `失败模式` 列表 → 触发对应 sub-skill
- [ ] 与 `ActionExecutor` 集成：高 confidence（>0.9）的 skill 直接执行，低 confidence 走 LLM

**产出：`src/skills/loader.py`, `src/skills/matcher.py`, `bots/<name>/skills/` 示例**

---

### Phase 5b: Roadmap P0 — 图数据库动作图 (2-3 天，可推迟）

**目标：当 skill 文件 >100 个后，用 networkx 图结构管理 skill 间的转移关系。**

> 前置条件：Phase 5a 已稳定运行，skill 文件 >100 个，关键词匹配开始出现歧义。
> Phase 5a 的文件层保留——图数据库是索引层，文件是数据源。

对应 `roadmap/01-action-state-graph.md`：

- [ ] 从 skill 文件自动构建 `StateNode` 和 `ActionEdge`
- [ ] `StateNode`：从 skill 的 `triggers.state` 字段自动提取
- [ ] `ActionEdge`：从 skill 的 `失败模式` 字段自动构建转移边
- [ ] 图查询：当前状态 → 候选边 → 按 confidence 排序
- [ ] 权重更新：执行后更新 skill 文件的 success/fail 计数
- [ ] 阈值控制：高于 threshold 直接执行，低于走 LLM

**产出：`src/graph/action_graph.py`**

### Phase 6: Roadmap P2 — ProceduralMemory 树形任务记忆 (2-3 天)

**目标：复杂任务可复用子树结构，减少重复规划。**

对应 `roadmap/03-task-tree.md`：

- [ ] `TaskNode` 数据结构：goal + subtasks + dependencies + executions + learned_rules
- [ ] 树检索：当前任务 embedding vs 历史任务 goal + learned_rules
- [ ] 模板注入：匹配到的任务结构注入 LLM prompt
- [ ] 执行记录更新
- [ ] 任务树作为 procedural memory 写入 MemoryLayer，同时保留 `bots/<name>/tasks/` JSON 导出便于调试

**产出：`src/task/task_tree.py`**

### Phase 7: Roadmap P3 — SemanticMemory 知识图谱 (2-3 天)

**目标：消除 LLM 对 Minecraft 配方的幻觉。**

对应 `roadmap/04-knowledge-graph.md`：

- [ ] 从 `minecraft-data` 解析配方/属性，自动建图
- [ ] `networkx` 图存储：BFS 查询合成路径、采集路径
- [ ] 2-hop 子图序列化注入 system prompt
- [ ] 运行时更新 `LOCATED_AT` / `STORED_IN` 边，并标记来源、置信度、世界 ID 与过期策略

**产出：`src/graph/knowledge_graph.py`**

### Phase 8: NPC 控制迁移 (2-3 天)

**目标：多 bot 管理逻辑迁入 Python。**

迁移 `npc/` 目录 (~500 行)：

- [ ] `NPCManager`：管理多个 bot 实例的生命周期
- [ ] `NPCController`：指令转发、状态同步
- [ ] 与 `mc_bridge` 的多实例适配

**产出：`src/npc/manager.py`, `src/npc/controller.py`**

### Phase 9: 集成测试与性能验证 (3-5 天)

- [ ] 端到端回归测试：采集、合成、建造、战斗
- [ ] MemoryLayer 回归测试：写入、检索、压缩、命名空间隔离、schema migration
- [ ] 多 bot 并发测试
- [ ] 与现有 Node.js 版本的 A/B 对比（延迟、token 消耗、成功率）
- [ ] 文档：迁移指南、开发环境搭建

## 风险矩阵

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| ~~JSPyBridge 高频事件性能瓶颈~~ | ~~中~~ | ~~高~~ | **已解决** — 改用 TCP Socket 桥接，JSON-line 协议 |
| ~~JSPyBridge 跨语言调试困难~~ | ~~高~~ | ~~中~~ | **已解决** — Socket 协议可独立抓包调试 |
| litellm 覆盖不全某些 provider | 低 | 中 | 保留自定义 adapter 扩展能力 |
| 记忆层过早复杂化 | 中 | 中 | Phase 3 只实现稳定接口 + SQLite 原型，Postgres/pgvector 保持可替换但不提前引入 |
| 多套记忆重复写入导致上下文污染 | 中 | 高 | 统一通过 MemoryLayer policy 写入，记录 source/confidence/tags/ttl，定期 compact |
| 图数据内存膨胀 | 低 | 低 | 设置节点/边上限，LRU 淘汰，底层持久化交给 MemoryLayer |
| 双版本维护负担（过渡期） | 高 | 中 | 以 feature flag 控制切换，逐步下线 Node.js agent 逻辑 |

## 时间线

```
Week 1:  Phase 0 (基础设施) ✅ + Phase 1 (LLM 层) ✅ + Phase 2 (Agent 核心) ✅
Week 2:  Phase 3 (MemoryLayer 接口) + Phase 4a (文件型记忆 MEMORY.md)
Week 3:  Phase 5a (文件型动作图 skill.md) + Phase 6 (P2 任务树)
Week 4:  Phase 7 (P3 知识图谱) + Phase 8 (NPC)
Week 5:  Phase 4b (FAISS 检索，按需) + Phase 5b (图数据库，按需)
Week 6:  Phase 9 (集成测试与性能验证)
```

**总计：约 6 周。Phase 4a/5a 用文件系统先跑通，比原计划节省约 1 周，且降低了过早抽象的返工风险。4b/5b 推迟到规模真正需要时引入。**

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
│   │   ├── service.py         # 统一读写、检索、压缩入口
│   │   └── policies.py        # 写入、晋升、遗忘、注入策略
│   ├── skills/
│   │   ├── loader.py          # Phase 5a: Skill 文件解析（YAML frontmatter + markdown）
│   │   └── matcher.py         # Phase 5a: 触发词 + 状态条件匹配器
│   ├── retrieval/
│   │   ├── faiss_index.py     # Phase 4b: 稠密向量索引（可推迟）
│   │   ├── bm25_index.py      # Phase 4b: 稀疏关键词索引（可推迟）
│   │   └── fusion.py          # Phase 4b: RRF 融合（可推迟）
│   ├── graph/
│   │   ├── action_graph.py    # Phase 5b: 动作-状态图（从 skill 文件自动构建，可推迟）
│   │   └── knowledge_graph.py # P3: 知识图谱
│   ├── task/
│   │   └── task_tree.py       # P2: 任务树
│   └── npc/
│       ├── manager.py         # 多 bot 管理
│       └── controller.py      # 指令转发
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
│   └── skills/
│       ├── mine_diamonds.md    # 技能模板
│       └── build_shelter.md
├── node_modules/              # Node.js 依赖（socket_server.js）
    ├── skills.js              # 动作执行（退化版）
    └── world.js               # 世界状态查询
```
