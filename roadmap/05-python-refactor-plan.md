# Python 重构实施方案

## 技术栈

| 层 | 技术 | 理由 |
|---|---|---|
| Minecraft 协议 | Mineflayer (Node.js) via JSPyBridge | 官方 examples/python/ 已支持，无需重写 |
| 桥接 | `pip install javascript` (JSPyBridge) | 一行 `require('mineflayer')`，替代 WebSocket |
| 类型系统 | Pydantic v2 + mypy | 所有数据结构建模，运行时校验 |
| LLM 调用 | litellm / 保留现有 provider adapter | 统一 20+ provider 接口，Python 生态更强 |
| 向量检索 | FAISS + rank-bm25 | 替代手写 cosine 排序，双层混合检索 |
| 图存储 | networkx | 动作状态图 + 知识图谱，纯 Python 无外部依赖 |
| 任务编排 | 自定义 TaskTree + langgraph（可选） | 树形任务记忆，可选升级为 DAG 编排 |
| 多进程 | asyncio + multiprocessing | 替代 child_process.spawn |
| 包管理 | uv / poetry | 快速依赖解析，lock 文件 |

## 架构总览

```
┌──────────────────────────────────────────────────┐
│  Python 层 (agent 大脑)                           │
│                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ prompter │ │ retrieval│ │ agent_core       │ │
│  │ (LLM)    │ │ (FAISS+  │ │ (Pydantic models │ │
│  │          │ │  BM25)   │ │  + decision loop)│ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ P0:      │ │ P1:      │ │ P2: task_tree    │ │
│  │ action_  │ │ retrieval│ │ P3: knowledge_   │ │
│  │ graph    │ │ _engine  │ │ graph            │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ npc_ctrl │ │ modes    │ │ conversation     │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
│                                                  │
│       │ JSPyBridge (from javascript import)      │
│       ▼                                          │
│  ┌──────────────────────────────────────────┐   │
│  │ Mineflayer (Node.js 运行时)               │   │
│  │ ├── bot 连接管理                          │   │
│  │ ├── pathfinder / collectBlock / pvp      │   │
│  │ ├── 事件系统 (chat, spawn, tick...)      │   │
│  │ └── skills.js 动作执行                    │   │
│  └──────────────────────────────────────────┘   │
└──────────────────────────────────────────────────┘
```

## 分层迁移步骤

### Phase 0: 基础设施搭建 (2-3 天)

**目标：Python 能控制 Mineflayer，跑通一个 echo bot。**

- [ ] 初始化 Python 项目结构 (`pyproject.toml`, `uv.lock`)
- [ ] `pip install javascript` 验证 JSPyBridge 可用
- [ ] 编写 `mc_bridge.py`：封装 `require('mineflayer')` 及插件加载
- [ ] 编写 `bot_events.py`：`@On(bot, "spawn")` / `@On(bot, "chat")` 事件注册
- [ ] 跑通 basic.py 级别的 echo bot，确认事件回调正常

**产出：`src/mc/bridge.py`, `src/mc/events.py`**

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

### Phase 3: Roadmap P1 — 双层检索系统 (2-3 天)

**目标：FAISS + BM25 混合检索替换手写 cosine 排序。**

对应 `roadmap/02-dual-retrieval.md`：

- [ ] FAISS 向量索引：启动时加载缓存，增量更新
- [ ] BM25 稀疏索引 (`rank-bm25`)
- [ ] RRF (Reciprocal Rank Fusion) 融合打分
- [ ] 向量缓存持久化 (`embedding_cache.json`)
- [ ] 替换 `examples.js` 和 `skill_library.js` 的检索逻辑

**产出：`src/retrieval/faiss_index.py`, `src/retrieval/bm25_index.py`, `src/retrieval/fusion.py`**

### Phase 4: Roadmap P0 — 动作-状态转移图 (3-4 天)

**目标：高频动作路径免 LLM，毫秒级响应。**

对应 `roadmap/01-action-state-graph.md`：

- [ ] `StateNode`：库存快照 + 附近实体 + embedding
- [ ] `ActionEdge`：命令 + 参数 + 成功率 + 前置条件 + 副作用
- [ ] 图查询：当前状态 embedding → 最近节点 → 最高权重边
- [ ] 权重更新：成功/失败计数 + 遗忘因子 (0.99)
- [ ] 阈值控制：高于 threshold 直接执行，低于走 LLM
- [ ] JSON 文件持久化

**产出：`src/graph/action_graph.py`**

### Phase 5: Roadmap P2 — 树形任务记忆 (2-3 天)

**目标：复杂任务可复用子树结构，减少重复规划。**

对应 `roadmap/03-task-tree.md`：

- [ ] `TaskNode` 数据结构：goal + subtasks + dependencies + executions + learned_rules
- [ ] 树检索：当前任务 embedding vs 历史任务 goal + learned_rules
- [ ] 模板注入：匹配到的任务结构注入 LLM prompt
- [ ] 执行记录更新
- [ ] 文件持久化 (`bots/<name>/tasks/`)

**产出：`src/task/task_tree.py`**

### Phase 6: Roadmap P3 — 知识图谱 (2-3 天)

**目标：消除 LLM 对 Minecraft 配方的幻觉。**

对应 `roadmap/04-knowledge-graph.md`：

- [ ] 从 `minecraft-data` 解析配方/属性，自动建图
- [ ] `networkx` 图存储：BFS 查询合成路径、采集路径
- [ ] 2-hop 子图序列化注入 system prompt
- [ ] 运行时更新 `LOCATED_AT` / `STORED_IN` 边

**产出：`src/graph/knowledge_graph.py`**

### Phase 7: NPC 控制迁移 (2-3 天)

**目标：多 bot 管理逻辑迁入 Python。**

迁移 `npc/` 目录 (~500 行)：

- [ ] `NPCManager`：管理多个 bot 实例的生命周期
- [ ] `NPCController`：指令转发、状态同步
- [ ] 与 `mc_bridge` 的多实例适配

**产出：`src/npc/manager.py`, `src/npc/controller.py`**

### Phase 8: 集成测试与性能验证 (3-5 天)

- [ ] JSPyBridge 高频 tick 场景压测（modes.js 的 tick 级反应）
- [ ] 端到端回归测试：采集、合成、建造、战斗
- [ ] 多 bot 并发测试
- [ ] 与现有 Node.js 版本的 A/B 对比（延迟、token 消耗、成功率）
- [ ] 文档：迁移指南、开发环境搭建

## 风险矩阵

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| JSPyBridge 高频事件性能瓶颈 | 中 | 高 | Phase 0 即做 tick 压测，不通过则退回 WebSocket 方案 |
| JSPyBridge 跨语言调试困难 | 高 | 中 | 统一日志层，Python 侧捕获 JS 异常栈 |
| litellm 覆盖不全某些 provider | 低 | 中 | 保留自定义 adapter 扩展能力 |
| 图数据内存膨胀 | 低 | 低 | 设置节点/边上限，LRU 淘汰 |
| 双版本维护负担（过渡期） | 高 | 中 | 以 feature flag 控制切换，逐步下线 Node.js agent 逻辑 |

## 时间线

```
Week 1:  Phase 0 (基础设施) + Phase 1 (LLM 层)
Week 2:  Phase 2 (Agent 核心) 
Week 3:  Phase 3 (P1 检索) + Phase 4 (P0 动作图)
Week 4:  Phase 5 (P2 任务树) + Phase 6 (P3 知识图谱)
Week 5:  Phase 7 (NPC) + Phase 8 (集成测试)
```

**总计：约 5 周（单人在 AI 辅助下），现实预估 5-6 周。**

## 目录结构（目标）

```
mindcraft-py/
├── pyproject.toml
├── uv.lock
├── src/
│   ├── mc/
│   │   ├── bridge.py          # JSPyBridge 封装
│   │   └── events.py          # Mineflayer 事件注册
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
│   ├── test_retrieval/
│   └── test_integration/
├── bots/                      # 各 bot 数据持久化
└── nodejs/                    # 保留的 Node.js 层
    ├── skills.js              # 动作执行（退化版）
    └── world.js               # 世界状态查询
```
