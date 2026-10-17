# 未来集成计划

## 1. Minecraft Wiki 知识检索（searchWiki）

### 当前状态
`searchWiki` 目前实现为一个基本的 HTML 抓取器，获取 Minecraft Wiki 页面并剥离 HTML 标签提取纯文本。它能够处理简单查询，但存在显著局限：

- 无法提取结构化数据（合成配方、方块属性、生物属性）
- 粗暴的 HTML 剥离导致语义结构丢失（表格、列表、标题）
- 无缓存 —— 每次查询都重复请求 wiki
- 无搜索消歧 —— 如果精确页面不存在，直接返回 404

### 建议架构

```
LLM 智能体 → !searchWiki("附魔台")
          → WikiClient (Python)
               ├─ 缓存层（SQLite 或磁盘文件）
               ├─ 获取 + 解析（aiohttp + beautifulsoup4）
               └─ 按页面类型的结构化提取器
          → 结构化结果 → LLM 上下文
```

### 实施计划

**阶段 A — 健壮的获取与解析（1-2 天）**
- 将 `urllib` 替换为 `aiohttp` 实现异步 HTTP
- 引入 `beautifulsoup4` 进行正确的 HTML 解析
- 通过信息框解析实现页面类型检测（物品、方块、生物、机制）
- 提取结构化数据：
  - **物品**：堆叠大小、耐久度、是否可再生
  - **方块**：硬度、所需工具、爆炸抗性、可燃性
  - **生物**：生命值、伤害、生成条件、掉落物
  - **合成配方**：材料网格 → 有序/无序配方
- 提取前 2-3 段描述文本

**阶段 B — 搜索与消歧（1 天）**
- 使用 wiki 的搜索 API（`/w/index.php?search=...`）处理模糊查询
- 当精确匹配失败时返回前 3 个搜索结果
- 让 LLM 自行选择或自动选取第一个结果

**阶段 C — 缓存（0.5 天）**
- SQLite 缓存，按页面名称 + 版本索引
- 基于 TTL 的过期机制（默认 24 小时）
- 预置常用页面缓存（工作台、熔炉等）

**阶段 D — 知识集成（1-2 天）**
- 将 wiki 文章嵌入到向量存储中（复用技能检索的嵌入模型）
- 允许 LLM 查询："合成附魔台需要什么材料？"
- 检索相关 wiki 章节并作为 `$WIKI_CONTEXT` 注入系统提示
- 这将 searchWiki 从一次性查询转变为持久知识层

### 关键设计决策
| 决策 | 建议 | 理由 |
|---|---|---|
| 解析库 | beautifulsoup4 | 成熟稳定，能处理不规范的 HTML，表格提取能力强 |
| 缓存后端 | SQLite | 零配置，单文件，随处可用 |
| 嵌入模型 | 与第三阶段记忆模块共用 | 复用现有嵌入基础设施 |
| 搜索 API | Minecraft Wiki 内置搜索 | 避免抓取搜索结果页，使用官方 API 端点 |

---

## 2. 蓝图建造系统

### 当前状态（JS）
原始 JS 项目在 `src/agent/tasks/construction_tasks.js` 和 `src/agent/npc/build_goal.js` 中有一个蓝图系统，功能包括：
- 将建筑模板定义为 3D 方块数组
- 验证建造场地（地面平整、足够空间）
- 逐层追踪建造进度
- 支持多智能体协作（一个机器人采集，另一个建造）

该系统**尚未移植**到 Python。

### 建议架构

```
蓝图（YAML 定义）
    │
    ▼
BlueprintValidator —— 检查场地适用性、材料
    │
    ▼
BuildPlanner —— 将蓝图分解为建造步骤
    │
    ▼
BuildExecutor —— 调度 !placeHere、!collectBlocks、!moveTo
    │                    │
    ▼                    ▼
ProgressTracker     InventoryManager
（保存状态）         （跟踪材料）
```

### 蓝图格式（YAML）

```yaml
# blueprints/small_house.yaml
name: 小型橡木屋
description: 带门和窗户的 5x5 橡木房屋
dimensions: [5, 4, 5]  # 宽, 高, 深
materials:
  oak_log: 24
  oak_planks: 32
  glass_pane: 8
  oak_door: 1
layers:
  - level: 0  # 地基
    grid: |
      L L L L L
      L . . . L
      L . . . L
      L . . . L
      L L L L L
  - level: 1  # 墙壁
    grid: |
      P P P P P
      P . . . P
      P . D . P
      P . . . P
      P P P P P
  - level: 2  # 窗户
    grid: |
      P P P P P
      P G . G P
      P . . . P
      P G . G P
      P P P P P
  - level: 3  # 屋顶
    grid: |
      . . P . .
      . P P P .
      P P P P P
      . P P P .
      . . P . .
legend:
  L: oak_log
  P: oak_planks
  G: glass_pane
  D: oak_door
  ".": air
```

### 实施计划

**阶段 A — 蓝图格式与解析器（1 天）**
- 定义蓝图的 YAML 模式
- 编写 `BlueprintLoader` 解析 YAML、验证结构
- 支持 `legend`（图例）以实现可读的网格定义
- 对照 `minecraft-data` 验证方块类型

**阶段 B — 场地验证器（1 天）**
- `BlueprintValidator.check_site(bridge, blueprint, position)`：
  - 验证占地范围内的地面是否平整
  - 检查建造区域上方的空间余量
  - 验证占地范围内无受保护方块（箱子、床等）
  - 返回问题列表或 OK

**阶段 C — 建造规划器（1-2 天）**
- `BuildPlanner.plan(blueprint, inventory)`：
  - 计算总需材料 vs 现有材料
  - 如果材料不足，使用 `getCraftingPlan` 生成采集计划
  - 将建造分解为有序步骤（自底向上、逐层进行）
  - 每个步骤 = 一个或多个 `!placeHere` / `!collectBlocks` 命令
  - 处理特殊方块（门需要正确朝向，火把需要依附墙壁）

**阶段 D — 建造执行器（1-2 天）**
- `BuildExecutor.run(bridge, plan)`：
  - 按顺序执行步骤并追踪进度
  - 错误处理（无法放置方块 → 检查原因、重试或跳过）
  - 将进度保存到磁盘，使建造进度在重启后仍然保留
  - 进度报告："已建造第 2/4 层，完成 67%"

**阶段 E — 自我提示集成（1 天）**
- 将蓝图接入目标系统：
  - `!setGoal "在 (100, 64, 200) 建造一个小型橡木屋"`
  - 智能体检测目标中的蓝图名称 → 加载蓝图 → 开始执行
  - `!checkBlueprint` 返回当前建造进度
  - `!getBlueprint` 打印蓝图描述

### 关键设计决策
| 决策 | 建议 | 理由 |
|---|---|---|
| 蓝图格式 | YAML | 人类可读写，Python 支持成熟 |
| 进度持久化 | 每个建筑一个 JSON 文件 | 简单，重启后保留，易于检查 |
| 材料来源 | 利用 `getCraftingPlan` | 复用现有技能，可组合 |
| 多智能体 | 推迟到阶段 F | 先单机器人验证正确性 |
| 方块朝向 | 蓝图中显式指明 | 门、楼梯、火把需要朝向信息 |

### 依赖项
- `pyyaml` — YAML 解析（Python 项目常用依赖）
- `numpy` — 可选，用于 3D 网格操作（也可用普通列表）

### 工作量估算
| 阶段 | 天数 | 描述 |
|---|---|---|
| A — 格式与解析器 | 1 | YAML 模式、加载器、验证 |
| B — 场地验证器 | 1 | 地面/空间检查 |
| C — 建造规划器 | 1-2 | 材料计算、步骤排序 |
| D — 建造执行器 | 1-2 | 执行循环、错误处理、进度追踪 |
| E — 自我提示集成 | 1 | 目标集成、状态命令 |
| **合计** | **5-7** | |

---

## 3. 剩余系统（尚未移植）

以下 JS 系统留待未来考虑，优先级较低：

| 系统 | 工作量 | 价值 | 说明 |
|---|---|---|---|
| **MemoryBank（记忆库）** | 2 天 | 中 | 命名位置存储；`!rememberHere`、`!goToRememberedPlace` 所需 |
| **Coder + SES** | 5+ 天 | 低 | 动态代码生成；安全风险高，实用价值有限 |
| **SkillLibrary（技能库）** | 3 天 | 高 | 基于嵌入向量的技能检索；是实现更智能 LLM 技能选择的关键 |
| **Vision 视觉系统** | 3 天 | 中 | 截图 + 视觉模型；用于视觉理解 |
| **NPC 系统** | 5+ 天 | 低 | 带有建造/物品目标的自主 NPC；复杂且使用场景有限 |
| **fullState 完整状态** | 1 天 | 中 | 全面的 JSON 状态导出；对调试很有价值 |
| **反应模式** | 3 天 | 中 | cowardice、self_defense、hunting、item_collecting、torch_placing 模式 |
