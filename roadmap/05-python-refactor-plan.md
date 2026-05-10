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

### Phase 4: Roadmap P1 — 双层检索系统接入 MemoryLayer (2-3 天)

**目标：FAISS + BM25 混合检索替换手写 cosine 排序。**

对应 `roadmap/02-dual-retrieval.md`：

- [ ] FAISS 向量索引：启动时加载缓存，增量更新
- [ ] BM25 稀疏索引 (`rank-bm25`)
- [ ] RRF (Reciprocal Rank Fusion) 融合打分
- [ ] 向量缓存持久化 (`embedding_cache.json`)
- [ ] 替换 `examples.js` 和 `skill_library.js` 的检索逻辑
- [ ] 将检索能力注册为 MemoryLayer 的 `search` backend，供 episodic/semantic/procedural 共用

**产出：`src/retrieval/faiss_index.py`, `src/retrieval/bm25_index.py`, `src/retrieval/fusion.py`**

### Phase 5: Roadmap P0 — ProceduralMemory 动作-状态转移图 (3-4 天)

**目标：高频动作路径免 LLM，毫秒级响应。**

对应 `roadmap/01-action-state-graph.md`：

- [ ] `StateNode`：库存快照 + 附近实体 + embedding
- [ ] `ActionEdge`：命令 + 参数 + 成功率 + 前置条件 + 副作用
- [ ] 图查询：当前状态 embedding → 最近节点 → 最高权重边
- [ ] 权重更新：成功/失败计数 + 遗忘因子 (0.99)
- [ ] 阈值控制：高于 threshold 直接执行，低于走 LLM
- [ ] 动作边与状态节点通过 MemoryLayer 持久化，支持按 bot/world/version 命名空间隔离

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
Week 1:  Phase 0 (基础设施) + Phase 1 (LLM 层)
Week 2:  Phase 2 (Agent 核心) 
Week 3:  Phase 3 (MemoryLayer) + Phase 4 (P1 检索)
Week 4:  Phase 5 (P0 动作图) + Phase 6 (P2 任务树)
Week 5:  Phase 7 (P3 知识图谱) + Phase 8 (NPC)
Week 6:  Phase 9 (集成测试与性能验证)
```

**总计：约 6 周（单人在 AI 辅助下）。新增 MemoryLayer 会多花约 1 周，但能显著降低后续记忆系统返工风险。**

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
│   │   ├── store.py           # MemoryStore 接口与 SQLite 实现
│   │   ├── service.py         # 统一读写、检索、压缩入口
│   │   └── policies.py        # 写入、晋升、遗忘、注入策略
│   ├── retrieval/
│   │   ├── faiss_index.py     # 稠密向量索引
│   │   ├── bm25_index.py      # 稀疏关键词索引
│   │   └── fusion.py          # RRF 融合
│   ├── graph/
│   │   ├── action_graph.py    # P0: 动作-状态图
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
├── bots/                      # 各 bot 数据持久化
└── nodejs/                    # 保留的 Node.js 层
    ├── skills.js              # 动作执行（退化版）
    └── world.js               # 世界状态查询
```
