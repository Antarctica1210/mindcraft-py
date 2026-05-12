# Python 重构实施方案

## 技术栈

| 层 | 技术 | 理由 |
|---|---|---|
| Minecraft 协议 | Mineflayer (Node.js) via JSPyBridge | 官方 examples/python/ 已支持，无需重写 |
| 桥接 | TCP Socket (JSON-line 协议) | 自建 Node.js TCP 服务端包裹 Mineflayer + Python async 客户端，替代 JSPyBridge |
| 类型系统 | Pydantic v2 + mypy | 所有数据结构建模，运行时校验 |
| LLM 调用 | langchain-openai + langchain-core (ChatOpenAI) | 统一 provider 接口，LCEL chain 模式 (prompt \| model \| parser)，OpenAI 兼容即可用 |
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
│  │ (LangCh  │ │ (files,  │ │ (Pydantic models │ │
│  │  ain)    │ │ later idx│ │  + decision loop)│ │
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
- 不适合：发明不存在的命令、绕过 `ToolCallParser`、绕过 `InputRouter` 自行判断是问答还是行动、直接操作 socket 或 Mineflayer API
- 实时性：LLM 调用必须可取消/超时；前台安全中断不等待模型返回

迁移 `models/prompter.js` (~800 行，含 20+ provider)：

- [x] 用 LangChain ChatOpenAI 统一 provider 接口（任何 OpenAI 兼容 endpoint，含 LM Studio / DeepSeek / Qwen）
- [x] 实现 Pydantic 类型化 I/O：`LLMInput`（system_message + history + tool_choice） → `LLMOutput`（content + tool_calls + usage + finish_reason），无 **kwargs
- [x] 实现 LCEL chain 模式：`ChatPromptTemplate | ChatOpenAI.bind(tools=...) | LLMOutputParser`
- [x] `LLMOutputParser` 继承 `BaseOutputParser`，将 LangChain 的 AIMessage.tool_calls 扁平格式转回 OpenAI 嵌套格式
- [x] 实现 `MessageBuilder`：system prompt + examples + conversation + tool definitions 拼接
- [x] 实现 `ToolCallParser`：解析 LLM 返回的 tool call，**1:1 映射到现有 `SocketBridge` 方法**（详见下文 SocketBridge 集成）
- [x] 实现 `InputRouter`：玩家消息进入主 LLM **之前**先做意图分类（详见下文）
- [x] 保留 history 压缩逻辑，用 Python 重写
- [x] 保留 context-window 恢复（trim oldest turns）+ transient error 重试（3 attempts）
- [x] 保留 think-tag 剥离（推理模型）
- [x] Embedding 改用 `OpenAIEmbeddings`
- [ ] 单元测试：各 provider 的 mock 调用验证；InputRouter 意图分类准确率验证；ToolCallParser 对 SocketBridge 全部方法签名的覆盖率验证

**SocketBridge 集成（贴现有实现，避免重复造轮子）：**

Phase 0 已交付 [src/mc/socket_bridge.py](src/mc/socket_bridge.py)，提供完整 async 客户端，~50 个方法按 SocketBridge 现有分组：

`lifecycle` (create_bot, close_server) · `observation` (observe, get_entities, get_nearby_blocks, get_craftable, get_inventory, get_goal) · `communication` (chat) · `movement` (move_to, go_to_player, follow_player, move_away, search_for_block, search_for_entity, go_to_bed, go_to_surface, dig_down, stay, set_control_state) · `combat` (attack, attack_player) · `inventory` (equip, consume, discard) · `block` (place_block, activate_block, use_on, collect_blocks) · `chest` (view_chest, put_in_chest, take_from_chest) · `crafting` (craft_recipe, smelt_item, clear_furnace, get_crafting_plan) · `villager` (show_villager_trades, trade_with_villager) · `look` (look_at_player, look_at_position) · `give` (give_to_player) · `skills` (pickup_nearby_items, should_place_torch, place_torch, till_and_sow, use_door) · `stop`

Phase 1 不重新发明工具协议，直接对接：

- [ ] **工具定义自动生成**：从 `SocketBridge` 方法签名（async def + 类型注解）反射出 OpenAI/Anthropic 兼容的 tool schema；新增方法时无需手改 prompter
- [ ] **工具分组**与 SocketBridge 现有分组保持一致，按需注入（采集任务只注入 observation+movement+block+inventory，战斗注入 combat+observation，避免 prompt 膨胀）
- [ ] **`ToolCallParser` 输出 `BridgeCall(method: str, params: dict)`**，由 Phase 2 `ActionExecutor` 直接 `await getattr(bridge, call.method)(**call.params)` 分派，**禁止**自行实现 Mineflayer API
- [ ] **黑名单**：`create_bot` / `close_server` 不暴露给 LLM；`attack_player` 默认隐藏，需玩家显式启用
- [ ] **工具描述里写明状态约束**（如 "craft_recipe 需附近有 crafting_table"），让 LLM 自行避免确定性失败
- [ ] 旧 `bridge.py` / `events.py`（JSPyBridge）**不在 Phase 1 引用范围**，roadmap 已标已废弃

**InputRouter：玩家输入意图分类（在 LLM 之前）：**

当前设计盲点：所有玩家消息直接喂给主 LLM + tool definitions，由 LLM "自由发挥"决定产 tool call 还是纯回话。这会导致问句被误执行（"基地在哪？" → `!goto(base)`）、元命令被当成动作（`remember base` 表层像 `go to base`）、反馈被当成新指令（`don't` 触发另一个动作）。

`InputRouter` 在 LLM 调用前做一次**粗分类**，分四类四条路径：

| 意图类别 | 识别方式 | LLM 配置 | 下游 |
|---|---|---|---|
| **meta_command** | 关键词正则命中（`remember / forget / save skill / unforget / save route / prefer / avoid`） | **跳过主 LLM**，走轻量 LLM 生成结构化候选 | `MemoryService` 写入路径（Phase 4 / Phase 5 policy gate） |
| **feedback** | 关键词正则命中（`wrong / don't / too risky / never / 不对 / 别 / 太冒险`） | 轻量 LLM 生成 feedback candidate + 关联最近 action trace | `feedback/` candidate（Phase 4 中风险） |
| **action_command** | 默认（含动词性 + 目标对象）或 LLM 分类 | `tool_choice="auto"`，允许 action tools | `ActionExecutor`（Phase 2） |
| **question / chitchat** | 疑问句 / 招呼 / 无动作目标 | `tool_choice="none"`，**禁用** action tools；需事实先 `recall()` | 仅回话 |
| **ambiguous** | 短命令缺动词 / 词义重叠（如 `base`、`mine`） | LLM 必须**先反问澄清**，不直接执行 | 暂存到 Conversation queue，等下一轮 |

实现要点：

- [ ] `InputIntent` Pydantic 枚举：`meta_command | feedback | action_command | question | chitchat | ambiguous`
- [ ] `classify(message: str, state: BotState, recent_actions: list) -> InputIntent`：快路径用正则 + 关键词表；慢路径在歧义时调用轻量 LLM 做二次判定
- [ ] **快路径**（meta_command / feedback）：不进主 LLM，直接走 Phase 4/5 写入候选生成器，延迟 < 100ms，风险可控
- [ ] **慢路径**（action_command / question）：进主 LLM，但用 `tool_choice` 硬约束，杜绝问句触发动作
- [ ] **歧义**：返回 `ambiguous` 后由 `AgentLoop` 触发反问，反问轮次也走 InputRouter 重新分类
- [ ] L1 NDJSON `player_msg` 事件扩展 `intent` 字段，便于事后审计分类准确率
- [ ] 关键词表可热加载（`config/intent_keywords.yaml`），多语言支持（中/英分表）

**产出：`src/llm/prompter.py`（LangChain ChatOpenAI + Pydantic I/O + chain 模式）, `src/llm/message_builder.py`, `src/llm/tool_parser.py`, `src/llm/input_router.py`**

### Phase 2: Agent 核心 + 类型建模 (5-7 天)

**目标：Pydantic 建模所有核心概念，agent 决策循环跑通。**

**LLM 介入点：**

- 适合：高层 planning、从 BotState 生成下一步候选动作、解释执行失败并提出重试策略
- 不适合：判断事实真值（背包、坐标、生命值、附近实体）、实时危险处置、路径可达性计算
- 实时性：AgentLoop 前台先执行规则化安全检查，再决定是否调用 LLM；LLM 规划超时则 fallback 到 skill/默认策略/停止

迁移 `agent/agent.js` (~600 行)：

- [ ] Pydantic models：`BotState`, `Inventory`, `Position`, `Entity`, `Action`, `Task`——**`BotState` schema 严格对齐 [src/mc/socket_bridge.py](src/mc/socket_bridge.py) 的 `observe()` 返回结构**（health / food / position / gamemode / time_of_day / biome / entities_nearby / blocks_nearby / players_nearby），不重新发明字段
- [ ] `AgentLoop`：感知 (observe) → **意图分类 (route)** → 规划 (plan) → 执行 (execute) → 反馈 (feedback) 主循环
- [ ] **意图分发**：在 plan 之前调用 `InputRouter.classify()`（Phase 1），按返回意图走五条分支：
  - `meta_command` → 走 `MemoryService` 候选生成，不进 plan
  - `feedback` → 走 `feedback` candidate 生成 + 关联最近 action trace，不进 plan
  - `action_command` → 进 plan，主 LLM `tool_choice="auto"`
  - `question / chitchat` → 进 plan，主 LLM `tool_choice="none"`，需事实先 `recall()`
  - `ambiguous` → AgentLoop 触发反问，玩家下一轮输入重新经 InputRouter
- [ ] `ActionExecutor`：将 `BridgeCall(method, params)` 通过 `await getattr(bridge, call.method)(**call.params)` 分派到 `SocketBridge`，**不直接持有 Mineflayer API**
- [ ] 迁移 `conversation.js`：对话历史管理，turn 压缩；记录每条 `player_msg` 的 `intent` 字段
- [ ] 迁移 `modes.js`：自驱动模式 / 跟随模式 / 采集模式的状态机；参考 [src/mc/observer_mode.py](src/mc/observer_mode.py) 的模式 + threat detection + 评估循环（注意改写为 SocketBridge 路径，不再用 JSPyBridge）
- [ ] 集成测试：`"collect 5 oak_log"` 端到端跑通；`"你在哪"`不得触发 action；`remember base` 走 MemoryService 不走 ActionExecutor

**BotState 来源（observe 合成）：**

`AgentLoop.observe()` 一步合成 BotState，**全部走 SocketBridge async API**，不重复实现：

```python
state = BotState(
    **(await bridge.observe()),
    inventory=await bridge.get_inventory(),
    entities=await bridge.get_entities(),
    nearby_block_types=await bridge.get_nearby_blocks(distance=16),
    current_goal=await bridge.get_goal(),
)
```

并发发起这些请求（`asyncio.gather`）以降低单 tick 延迟；任一失败用上一 tick 缓存兜底，记 `safety_interrupt` 事件。

**事件双通道（请求-响应 vs server-push）：**

SocketBridge 是**双向通道**，AgentLoop 必须区分驱动来源：

| 通道 | 触发 | 入口 | 下游 |
|---|---|---|---|
| 请求-响应 | AgentLoop 主动 | `await bridge.observe()` / `await bridge.collect_blocks(...)` 等 | 写入 L1 `action_call` / `action_result` 事件 |
| Server-push | Mineflayer 事件 | `@bridge.on("chat" / "spawn" / "end" / "error" / "kicked")` decorator | `chat` → InputRouter；`spawn` / `end` / `kicked` → 重连或终止；`error` → safety_interrupt |

`@bridge.on("chat")` 是玩家输入的**唯一入口**，handler 不直接进 plan，先入 `Conversation.queue()`，由 AgentLoop 下一次循环统一经 InputRouter 处理（保证 InputRouter 路径是单一入口）。

**安全中断与高频感知：**

`socket_server.js` 当前只推送 `chat / spawn / end / error / kicked`——**没有** `physicsTick` 或低血量通知。Phase 2 必须明确选择：

- [ ] **方案 A（推荐起手）**：AgentLoop 起一个 `SafetyMonitor` async 任务，独立于主循环，固定 200-500ms 调一次 `bridge.observe()`，发现 health<5 / lava 接近 / 敌对 entity 距离 <3 时设置中断标志，主循环在 plan / execute 边界检查并跳到 emergency skill
- [ ] **方案 B（性能更好但需改 Node 端）**：扩展 `socket_server.js` 推送 `health_change` / `entity_hurt` / `block_update` 事件，AgentLoop 订阅这些事件直接触发中断；目前**不在 Phase 2 范围**，先列为可选优化
- [ ] LLM 调用必须可 `asyncio.cancel()`，safety_interrupt 触发时立即取消 plan 中的未完成 LLM 请求，跳到 emergency 路径

**产出：`src/agent/core.py`, `src/agent/models.py`, `src/agent/executor.py`, `src/agent/conversation.py`, `src/agent/modes.py`, `src/agent/safety_monitor.py`**

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
- [ ] 元数据规范：`scope` (必填，`minecraft` | `bot` | `world`，见下文 Scope 子节), `source`, `confidence`, `created_at`, `updated_at`, `ttl`, `world_id` (当 `scope=world` 时必填), `tags`, `evidence`, `valid_when`, `invalid_when`
- [ ] 存储抽象：只定义接口，不绑定具体存储；Phase 4a 的 `FileMemoryStore` 是第一个实现
- [ ] 检索抽象：只定义 `search(query) -> list[MemoryResult]`，不提前实现 dense vector / BM25 / RRF
- [ ] 写入策略：哪些事件自动写入、哪些需要 LLM 反思后晋升、哪些只保留短期
- [ ] 压缩策略：conversation summary、execution trace summary、失败原因归因
- [ ] skill 写入边界：自动更新统计字段，步骤/失败模式变更进入候选变更或人工确认
- [ ] 后台反思队列：前台记录事件，后台批量调用 LLM 总结并提交 policy gate

**Session History 三层模型（普通 action/对话历史的归宿）：**

`conversation.py` 当前的 `compress_history` 把超窗口的旧 turn 直接丢弃，没有落盘 sink；Phase 4a 的目录也只列了 `places/feedback/facts/`，没有"按时间发生过什么"的存放位置。后续 Phase 5 skill 候选、Phase 6 TaskRun、Phase 7b world facts 都需要从一条 raw trace 上切片，所以这层必须在 Phase 3 收敛接口时一起定义。

| 层 | 位置 | 写入时机 | 寿命 | 用途 |
|---|---|---|---|---|
| **L0 Working** | 内存 ring buffer（`Conversation.compress_history` 的窗口内容） | 每 turn / 每 action | 当前 session | 直接注入 prompt，前台决策 |
| **L1 Raw Trace** | `bots/<name>/sessions/<date>/<session_id>.ndjson` append-only | 每个 event 实时落盘 | 滚动 TTL（默认 30 天）后归档/删除 | reflection worker 的输入；调试回放；TaskRun / skill 候选的切片来源 |
| **L2 Episodic Summary** | `bots/<name>/memory/episodic/*.md`（沿用 Phase 4a YAML frontmatter + body） | session 结束 / 任务完成 / 失败 / 时间窗触发 reflection | 长期，Phase 4b 上线后建索引 | `MemoryService.recall()` 召回，注入 prompt |

**核心原则：** L1 是 truth source，L0 / L2 都是加速或衍生层；reflection 跑错可从 L1 重放，L2 损坏可重建。前台**只允许召回 L2 摘要注入 prompt**，永远不要把 L1 raw 直接回灌（体积大且会反复污染上下文）。

L1 NDJSON 每行一个事件，schema 建议：

```json
{"ts":"2026-05-11T14:30:12.345Z","session_id":"...","world_id":"main","type":"llm_turn|action_call|action_result|world_event|player_msg|safety_interrupt","intent":"meta_command|feedback|action_command|question|chitchat|ambiguous","payload":{...}}
```

`intent` 字段仅在 `type=player_msg` 时有效，由 Phase 1 `InputRouter` 写入；便于事后审计分类准确率与路径分布。

session_id 按"自然中断"切（玩家离线 / bot 重连 / 任务完成 / 安全死亡），不按固定时长，便于 TaskRun 和 skill 提取对齐。

实现内容（补充到上面的清单）：

- [ ] `MemoryService.record_event(event)`：fire-and-forget，前台不阻塞，直接 append 到 L1 NDJSON
- [ ] `EventSink` 协议：`Conversation.compress_history` 改造为先经 sink 再丢弃，老 turn 走 L1
- [ ] `BackgroundReflectionWorker`：消费 L1 切片，调用 LLM 生成 L2 候选 → 经 `MemoryPolicy` → 写入 `memory/episodic/`
- [ ] L1 滚动 TTL 与归档策略（默认 30 天），归档目录可压缩
- [ ] 写入风险分级：L1 raw = 低风险自动追加；L2 summary = 中风险走 policy；晋升到 `places/feedback/facts/` 复用 Phase 4 普通记忆规则；晋升到 skill 复用 Phase 5 高风险确认
- [ ] 注入策略：L0 全量原样注入；老历史只通过 `recall()` 拿 L2 摘要

下游消费关系（仅记录，具体实现放对应 Phase）：

- **Phase 5 skill 候选**：从 L1 找"重复 N 次成功的 action 序列"作为 candidate skill
- **Phase 6 TaskRun**：一次任务对应 L1 的一个时间切片，复盘压缩为 TaskRun JSON + learned_rules
- **Phase 7b world facts**：从 L1 的 `world_event` 去重提炼坐标/库存/危险区域等事实

**Working Memory 契约：**

`working` namespace 不写盘，纯内存对象，承担"当前一刻 bot 应该知道什么"。范围限定：

- 当前对话窗口（L0 ring buffer，沿用 `Conversation.compress_history`）
- 当前目标 / 当前计划（来自 AgentLoop）
- 当前 skill 上下文（被激活的 skill 文件内容 + 局部状态）
- 近期世界状态快照（BotState 最新切片：position / health / inventory / nearby_entities）

接口与责任：

- [ ] `MemoryService.get_working_context() -> WorkingContext`：唯一聚合入口，`MessageBuilder` 调它拿结构化对象，按 token 预算注入 prompt
- [ ] `WorkingContext` Pydantic 模型：明确字段，避免散落在 `BotState` / `Conversation` / `AgentLoop` 各处自由拼接
- [ ] 可选 crash-recovery 快照：`sessions/<sid>/working_snapshot.json`，每 N 秒或关键事件后写一次；重启时读回并校验有效性，失败则丢弃
- [ ] working 内容**不参与 recall()**：长期记忆走 episodic/semantic/procedural，working 只在 prompt 拼接阶段直接注入

**Reflection 触发与失败语义：**

- [ ] 触发条件（满足任一）：session 结束 / 任务完成 / 任务失败 / idle ≥ N 分钟（默认 5） / L1 累积 ≥ N 条（默认 200） / L1 体积 ≥ token 阈值
- [ ] 模型选择：默认轻量 LLM（cheap fast），仅在 high-stakes consolidation（晋升 feedback / 修 skill 候选）时升级到主 LLM
- [ ] token 预算：单次 reflection ≤ T tokens，超出则按 session_id 切片分批
- [ ] 失败处理：reflection 失败**不回写 L2**，但保留 L1；同一 session 失败 ≥ N 次（默认 3）→ 标记 `reflection_pending`，进 review queue，不阻塞新 session
- [ ] L1 归档锁：未完成 reflection 的 session 不可归档/删除，TTL 暂停
- [ ] 幂等性：同一 L1 切片重复 reflection 必须产出一致 candidate（写入前按 content hash 去重）

**Forgetting / Conflict 统一 Evaluator：**

`MemoryEvaluator` 作为周期任务（独立于 reflection worker），统一处理记忆生命周期：

- [ ] TTL 过期 → forget
- [ ] `invalid_when` 条件满足 → 降 confidence 或 forget
- [ ] confidence 衰减：未被 recall 命中的记忆按 half-life 衰减，命中则刷新
- [ ] 冲突检测：同 namespace + 同实体 + 互斥事实 → 触发 conflict resolution
- [ ] Conflict resolution 策略（按优先级）：
  1. 静态知识 > 世界事实（沿用 Phase 7b 原则）
  2. source 权重：`player_confirmed` > `player_marked` > `bot_observed` > `llm_reflected`
  3. recency × confidence 加权打分
  4. 无法决断 → 进 review queue，双方 confidence 同步降级
- [ ] Forget 持久性（tombstone / deny-list）：
  - `forget()` 同步写 `bots/<name>/memory/.tombstones.json`
  - Reflection 写入前先查 tombstone，被 forget 的实体/事实不得回写
  - 玩家可显式解除（`unforget <name>`）

**`semantic` / `facts/` / `KnowledgeGraph` 关系澄清：**

- `semantic` namespace 是**逻辑层**，由两个 backend 组成：
  - **Static backend**：Phase 7a `KnowledgeGraph`（minecraft-data 只读），无 record 写入
  - **Dynamic backend (World Facts)**：Phase 7b 写入 `memory/facts/`，对应 `MemoryRecord` 子类型
- `recall(namespace="semantic", ...)` 内部按需查两个 backend；冲突时 static 优先（见上）
- 玩家偏好、对话事实（非世界事实）单独放 `memory/facts/players.md` 等，仍属 `semantic` 但不进 KnowledgeGraph

**Scope 强制与召回路由（通用 vs 世界特定）：**

`scope` 是 `MemoryRecord` 和 skill frontmatter 的**必填字段**，三选一：

- `minecraft` — 通用 Minecraft 机制（配方、工具等级、怪物掉落、食物值），由 Phase 7a 静态知识承担；运行时不直接写入
- `bot` — 当前 bot 的通用技能、经验教训、跨世界偏好，**跨世界、可分享**（分享机制见 Phase 8b）
- `world` — 仅在当前 `world_id` 有效的事实（坐标、库存、玩家关系、危险区域、episodic、TaskRun）

**召回顺序：world > bot > minecraft**（更具体更可信）。同名冲突时 world override bot。

写入路由原则：

- `remember base` / 命名坐标 → 强制 `world`
- `save skill X` → 默认 `bot`；若候选内容含具体坐标 / 命名实体 / 玩家名 → 自动降级 `world` 并提醒玩家
- World facts（箱子、传送门、危险区域）→ 强制 `world`
- Reflection 提出的 feedback candidate → LLM 提议 scope，policy 复核
- 玩家偏好 → `bot` + `player_id` 标记
- `scope` 升级（`world → bot`，把世界特定内容提升为通用）必须玩家显式确认，防止坐标 / 命名实体污染通用层

实现内容（补到上面清单）：

- [ ] `MemoryRecord.scope` 必填字段（`Literal["minecraft", "bot", "world"]`）
- [ ] `MemoryQuery` 默认按 `world > bot > minecraft` 顺序聚合
- [ ] 写入前 scope 校验器：扫描内容是否含 world-specific 实体（坐标、`places/` 命名），违反时自动降级或拒绝
- [ ] Skill / 通用记忆**只描述流程**，具体坐标/箱子通过 `recall(scope="world", ...)` 在执行时拼接，**不允许硬编码进 `bot` 层文件**

**产出：`src/memory/models.py`, `src/memory/store.py`, `src/memory/service.py`, `src/memory/policies.py`, `src/memory/session_sink.py`, `src/memory/reflection_worker.py`, `src/memory/working.py`, `src/memory/evaluator.py`, `src/memory/tombstones.py`**

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
bots/<bot_name>/
├── universal/                ← scope=bot，跨世界，可分享（Phase 8b 打包）
│   ├── MEMORY.md             ← 通用记忆索引
│   └── memory/
│       └── feedback/
│           ├── night_caving.md  ← 教训：夜间无盾下洞危险（机制类，通用）
│           └── creeper_combat.md ← 教训：苦力怕近战策略
└── worlds/<world_id>/        ← scope=world（无 world_id 时默认 worlds/default/）
    └── memory/
        ├── MEMORY.md         ← 此世界的索引，始终加载（每行 <150 字符，上限 200 行）
        ├── episodic/         ← Phase 3 L2 episodic summary（非任务区间）
        │   ├── 2026-05-10_mining_run.md
        │   └── 2026-05-10_night_death.md
        ├── places/
        │   ├── base.md       ← 命名位置：基地坐标
        │   └── diamond_mine.md ← 命名位置：钻石矿坐标
        └── facts/
            ├── players.md    ← 玩家偏好、关系（属此世界/此服务器）
            └── world_facts.md ← 世界事实（传送门位置等）
```

> Skill 文件不放在 `memory/skills/`，而是独立存放在 `bots/<bot_name>/skills/`。它们属于 Procedural Memory，但 schema、写入风险和执行语义都不同，避免和普通事实/反馈记忆混写。

记忆文件格式（YAML frontmatter + markdown body）：

```markdown
---
name: base_location
description: 主基地坐标
type: place
scope: world              # 含坐标，强制 world 层
world_id: main
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
scope: bot                # 基于游戏机制，跨世界通用
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
<!-- bots/<name>/universal/skills/mine_diamonds.md -->
---
name: mine_diamonds
description: 寻找并挖掘钻石矿
scope: bot                # 流程通用，不含坐标 / 命名实体
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
- [ ] `SkillMatcher`：给定 situation + BotState → 返回最佳匹配 skill；按 `world > bot` scope 顺序匹配
- [ ] 权重自动更新：每次执行后更新 `success_count / fail_count / confidence`
- [ ] 步骤/失败模式变更策略：默认生成候选变更，高风险 skill 需要人工确认
- [ ] 失败链式回退：skill 执行失败 → 读取 `失败模式` 列表 → 触发对应 sub-skill
- [ ] 与 `ActionExecutor` 集成：高 confidence（>0.9）的 skill 直接执行，低 confidence 走 LLM
- [ ] **Scope 路由与流程/坐标解耦**（详见下文）

**Scope 路由与流程/坐标解耦：**

Skill 文件分两层存放，强制 `scope` 字段：

- **通用 skill**（默认）：`bots/<name>/universal/skills/*.md`，`scope: bot`——只描述流程、前置条件、失败模式，**不允许硬编码坐标 / 命名实体 / 玩家名**
- **世界特定 skill**（少数）：`bots/<name>/worlds/<world_id>/skills/*.md`，`scope: world`——绑定具体地图的特殊流程（如"基地巡逻路线"）

通用 skill 执行时通过 `recall(scope="world", ...)` 拿坐标 / 命名实体后再拼接动作：

```python
# 通用 mine_diamonds.md 不持有具体坐标
# 执行时 SkillExecutor 调 recall("places", "diamond_mine") 取坐标
loc = memory.recall(scope="world", type="place", name="diamond_mine")
if not loc:
    # 触发先勘探子 skill 或问玩家
    return need_exploration()
goto(loc.coords)
mine_down_to(y=-58)
```

匹配优先级：`world` scope skill > `bot` scope skill > LLM 兜底。`SkillMatcher` 同时扫两个目录，命名冲突时 world 覆盖 bot。

写入时静态校验：

- [ ] 写入 `scope: bot` 的 skill 前扫描内容，发现坐标字面量 / `places/` 命名引用 → 拒绝写入或自动降级到 `scope: world` 并提示
- [ ] 写入 `scope: world` skill 时正常通过，但 policy 仍要求显式确认
- [ ] `scope` 升级（world → bot，例如把私人流程贡献为通用）必须玩家显式确认，且强制去除坐标 / 命名实体

**产出：`src/skills/loader.py`, `src/skills/matcher.py`, `src/skills/scope_validator.py`, `bots/<name>/universal/skills/` + `bots/<name>/worlds/<world_id>/skills/` 示例**

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

**与 Phase 3 episodic 的边界（避免双写）：**

L1 切片在 reflection 阶段按是否归属已知 `task_id` 分流：

- **属于某 task_id** → 生成 / 更新 `bots/<name>/tasks/<task_id>.json`（TaskRun，含 trace summary / learned_rules / used_memories / used_skills），**不**再写 `memory/episodic/`
- **不属于任何 task** → 探索、自由活动、闲聊等，写 `memory/episodic/*.md`
- 召回时两类都可被检索，`MemoryService.recall()` 内部按需聚合；同一 trace 不允许同时出现两份 artifact

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

> 开发策略参考：`docs/minecraft-knowledge-strategy.md`。Phase 7b 只处理 World Facts：bot 在具体世界里观察到的事实必须经过 MemoryLayer policy 写入，并带 `scope: world`, `world_id`, `ttl`, `confidence`, `evidence`。

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
- [ ] 世界事实必须标记 `scope: world` + `world_id` + `source`, `confidence`, `evidence`, `ttl`, `valid_when`, `invalid_when`
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

### Phase 8b: 通用 Skill / Memory Bundle 分享 (Deferred，按需)

**目标：把 `scope: bot` 的通用 skill 和经验教训打包、清洗、分发给其他 bot / 玩家，让通用智能在社区内累积。`scope: world` 内容永远不参与分享。**

> 暂不进入主线。前置条件：Phase 5a + Phase 4a 稳定，scope 校验已工作；社区 / 多玩家场景明确出现。

**LLM 介入点：**

- 适合：辅助生成 skill 的英文 description / 多语言摘要、检测分享前是否含隐私信息
- 不适合：决定信任级别、覆盖 quarantine 决策、绕过版本检查、直接 enable 高风险导入 skill
- 实时性：打包 / 校验 / 导入全程离线；导入后 skill 默认进 quarantine，需玩家显式 enable 后才参与匹配

设计原则：

- **只能分享 `scope: bot` 的内容**——`world` 层（坐标 / 库存 / 玩家关系）永远不出仓库
- **导入即降权**：confidence 重置（默认 0.5），靠本地复用次数升回
- **高风险 skill 强制 quarantine**：含 `digDown` / `break_block` / `attack_player` 等动作的导入 skill 默认 disabled，需玩家 `enable <skill>` 后才生效
- **版本固定**：每个 bundle 声明 Minecraft 版本 / mod 依赖 / `minecraft-data` 版本，导入时 mismatch 触发警告或拒绝
- **本地优先**：同名 skill 本地 override 导入版本；导入版本另存为 `<name>.shared.md`，仅在本地失败时 fallback

实现内容：

- [ ] **Bundle 格式**：`bundle.yaml` + `universal/` 子树（skills + feedback），SHA-256 hash 作为 bundle id
- [ ] **Sanitizer**：打包前扫描，禁止出现坐标字面量、`places/` 命名引用、`world_id`、玩家用户名等；命中即报错或自动剔除
- [ ] **Provenance 字段**（写入导入文件 frontmatter）：`source: imported`, `origin: <author/bundle_hash>`, `imported_at`, `peer_reviewed: N`, `local_verified: false`, `quarantined: true|false`
- [ ] **版本检查器**：比对当前 Minecraft 版本与 bundle 声明，mismatch 提示降级处理
- [ ] **冲突解决**：本地同名 skill 存在时，导入版本写到 `<name>.shared.md`；SkillMatcher 优先本地，本地失败再尝试 shared
- [ ] **Quarantine 控制**：`bundle enable <skill>` / `bundle disable <skill>` / `bundle remove <bundle_id>`
- [ ] **审计日志**：导入 / enable / 首次执行 / 失败案例均落 L1 NDJSON，便于追溯
- [ ] **CLI 工具**：`bundle export <name>` / `bundle import <path-or-url>` / `bundle list` / `bundle inspect <id>`

**风险与缓解：**

| 风险 | 缓解 |
|---|---|
| 导入 skill 含恶意动作（破坏建筑、攻击玩家） | quarantine 默认 disabled + 高风险动作清单 + dry-run 模拟 |
| 隐私泄漏（导出时夹带坐标 / 玩家名） | Sanitizer 强制扫描，命中阻塞导出 |
| 版本不兼容（1.20 skill 用在 1.21） | bundle.yaml 声明版本，导入时强制校验 |
| 错误经验被广泛分发（坏 feedback） | 导入 feedback 同样降权，需本地多次命中才升 confidence |
| 同名覆盖本地正确流程 | 本地 override，shared 仅作 fallback |

**产出：`src/bundle/exporter.py`, `src/bundle/importer.py`, `src/bundle/sanitizer.py`, `src/bundle/version_check.py`, `src/bundle/quarantine.py`（Deferred）**

### Phase 9: 集成测试与性能验证 (3-5 天)

- [ ] 端到端回归测试：采集、合成、建造、战斗
- [ ] **SocketBridge 工具覆盖测试**：ToolCallParser 必须能正确分派 SocketBridge 的全部公开方法；新增 SocketBridge 方法时自动 schema 反射要能覆盖
- [ ] **SafetyMonitor 测试**：构造 health<5 / lava / hostile 距离<3 场景，验证中断标志生效且 plan 中的 LLM 调用被 `asyncio.cancel()`
- [ ] **事件双通道测试**：`@bridge.on("chat")` 入口必须经 `Conversation.queue()` 再进 InputRouter，不直接进 plan；`spawn` / `end` / `error` / `kicked` 事件能触发对应重连/终止逻辑
- [ ] MemoryLayer 回归测试：写入、检索、压缩、命名空间隔离、schema migration
- [ ] **记忆 determinism 测试**：fixture L1 NDJSON → 多次运行 reflection → L2 候选必须稳定（剔除时间戳后 content hash 一致）
- [ ] **记忆老化测试**：TTL 过期、confidence half-life 衰减、`invalid_when` 触发——用快进时间夹具验证 `MemoryEvaluator` 行为
- [ ] **Forget 持久性测试**：`forget(X)` → 同 L1 trace 重放 reflection → 不得回写 X；`unforget(X)` 后恢复
- [ ] **Conflict resolution 测试**：构造静态/世界事实冲突、source 权重冲突、recency 冲突场景，验证 evaluator 决策路径
- [ ] **Working Memory 测试**：crash-recovery 快照写入/读回/失效场景；`get_working_context()` 在 token 预算下的截断行为
- [ ] **Scope 隔离测试**：`scope: bot` skill 含坐标字面量必须被 scope_validator 拒绝；`recall()` 必须按 `world > bot > minecraft` 顺序返回；命名冲突时 world override bot
- [ ] **InputRouter 意图分类测试**（关键，防误执行）：
  - 问句类（"你在哪 / 你怎么死的 / 现在几点"）→ 必须分类为 `question`，且 LLM 调用必须 `tool_choice="none"`，**不得**产生任何 action_call
  - 元命令类（`remember X / forget X / save skill X / unforget X`）→ 必须分类为 `meta_command`，走 `MemoryService` 路径，**不得**进 ActionExecutor
  - 反馈类（`wrong / don't / 不对 / 别 / too risky`）→ 必须分类为 `feedback`，生成 candidate 时**必须关联最近 N 个 action_call**
  - 行动类（"挖 10 个橡木 / 去基地 / 造一个箱子"）→ 必须分类为 `action_command`，主 LLM 应产 tool call
  - 歧义类（`base` 单独、`mine` 单独等）→ 必须分类为 `ambiguous`，AgentLoop 必须触发反问而非直接行动
  - 多语言：中文 / 英文表都需覆盖
  - 审计：L1 `player_msg.intent` 字段必须正确写入
- [ ] TaskTree 回归测试：任务复盘、模板检索、candidate skill 晋升、**与 episodic 不双写**
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
| ChatOpenAI 不兼容某些非 OpenAI-API provider | 低 | 中 | 需 Anthropic/Gemini 时接入 langchain-anthropic / langchain-google-genai |
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
Deferred: Phase 8 (多 bot / NPC 控制迁移，按需) + Phase 8b (通用 Skill / Memory Bundle 分享，按需)
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
│   │   ├── prompter.py        # LLM 调用统一接口 (LangChain ChatOpenAI + Pydantic I/O)
│   │   ├── message_builder.py # Prompt 拼接
│   │   ├── tool_parser.py     # Tool call 解析（LLM 输出端）
│   │   └── input_router.py    # Phase 1: 玩家输入意图分类（LLM 输入端，五类分发）
│   ├── agent/
│   │   ├── core.py            # Agent 主循环 (observe → route → plan → execute → feedback)
│   │   ├── models.py          # Pydantic 数据模型（BotState 对齐 SocketBridge.observe()）
│   │   ├── executor.py        # ActionExecutor：BridgeCall → SocketBridge 分派
│   │   ├── conversation.py    # 对话管理 + intent 字段记录
│   │   ├── modes.py           # 行为模式状态机（参考 observer_mode.py 模式）
│   │   └── safety_monitor.py  # Phase 2: 高频 observe 轮询 + 中断标志
│   ├── memory/
│   │   ├── models.py          # MemoryRecord / MemoryQuery / MemoryResult / WorkingContext
│   │   ├── store.py           # MemoryStore 接口
│   │   ├── file_store.py      # Phase 4a: 文件系统实现（MEMORY.md 索引 + .md 记忆文件）
│   │   ├── index.py           # Phase 4a: MEMORY.md 索引读写
│   │   ├── sqlite_store.py    # Phase 3b: SQLite 结构化索引/查询层（可推迟）
│   │   ├── session_sink.py    # Phase 3: L1 NDJSON append-only sink（record_event + EventSink 协议）
│   │   ├── reflection_worker.py # Phase 3: 后台 worker，消费 L1 → 生成 L2 episodic 候选
│   │   ├── working.py         # Phase 3: WorkingContext 聚合（对话窗口 + 当前目标/计划/skill/世界快照）
│   │   ├── evaluator.py       # Phase 3: TTL / confidence 衰减 / invalid_when / conflict resolution
│   │   ├── tombstones.py      # Phase 3: forget deny-list 持久化（防 reflection 回写）
│   │   ├── service.py         # 统一读写、检索、压缩入口
│   │   └── policies.py        # 写入、晋升、遗忘、注入策略
│   ├── skills/
│   │   ├── loader.py          # Phase 5a: Skill 文件解析（YAML frontmatter + markdown）
│   │   ├── store.py           # Phase 5a: ProceduralMemoryAdapter / SkillStore
│   │   ├── matcher.py         # Phase 5a: 触发词 + 状态条件匹配器（scope 路由）
│   │   └── scope_validator.py # Phase 5a: scope=bot skill 不得含坐标 / 命名实体
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
│   ├── npc/
│   │   ├── manager.py         # Deferred: 多 bot 管理
│   │   └── controller.py      # Deferred: 指令转发
│   └── bundle/                # Phase 8b: 通用 Skill / Memory 分享（Deferred）
│       ├── exporter.py        # 打包 universal/ 为 bundle
│       ├── importer.py        # 导入 bundle 到本地 universal/，强制 quarantine
│       ├── sanitizer.py       # 扫描坐标 / 玩家名 / 命名实体，阻塞泄漏
│       ├── version_check.py   # Minecraft 版本 / mod / minecraft-data 版本比对
│       └── quarantine.py      # 高风险 skill 默认 disabled，玩家显式 enable
├── tests/
│   ├── test_llm/
│   ├── test_agent/
│   ├── test_memory/
│   ├── test_retrieval/
│   └── test_integration/
├── bots/<name>/               # 每个 bot 的数据持久化
│   ├── universal/              # scope=bot —— 跨世界、可分享（Phase 8b 打包）
│   │   ├── MEMORY.md           # 通用记忆索引
│   │   ├── memory/
│   │   │   └── feedback/       # 机制类经验教训（"夜间无盾下洞危险"）
│   │   └── skills/             # 通用流程模板，不含坐标 / 命名实体
│   │       ├── mine_diamonds.md
│   │       └── build_shelter.md
│   └── worlds/<world_id>/      # scope=world —— 仅本世界（无 world_id 时默认 default/）
│       ├── memory/
│       │   ├── MEMORY.md           # 此世界的索引
│       │   ├── .tombstones.json    # Phase 3: forget deny-list
│       │   ├── episodic/           # Phase 3: L2 episodic summary
│       │   ├── places/             # 命名位置（基地、矿点）
│       │   └── facts/              # 世界事实（玩家关系、传送门、危险区）
│       ├── sessions/               # Phase 3: L1 raw trace，按天分目录
│       │   └── 2026-05-11/
│       │       ├── 1430-mining.ndjson
│       │       └── 1430-mining.working_snapshot.json
│       ├── skills/                 # 少数世界特定 skill（如基地巡逻路线）
│       └── tasks/
│           └── build_base_run.json # Phase 6: 任务复盘导出
├── data/
│   └── minecraft/<version>/    # Phase 7a: minecraft-data 导出的结构化知识
│       ├── items.json
│       ├── blocks.json
│       ├── recipes.json
│       ├── entities.json
│       └── foods.json
├── scripts/
│   └── export_minecraft_data.js # Phase 7a: 导出 minecraft-data JSON
├── config/
│   └── intent_keywords.yaml   # Phase 1: InputRouter 关键词表（中/英分表，热加载）
├── node_modules/              # Node.js 依赖（socket_server.js）
│   ├── skills.js              # 动作执行（退化版）
│   └── world.js               # 世界状态查询
```
