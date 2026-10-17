# Phase 收益评估样本

本文记录 Python 重构路线中各阶段对 bot 能力的预期提升，可作为后续评估、排期和验收样本。

## 推荐顺序

完整 Phase 7 不建议放到 Phase 3/4/5 前面，但可以拆出轻量版 Static Knowledge 提前做。

推荐顺序：

```text
Phase 1 -> Phase 2 -> Phase 3 light
  -> Phase 7a Static Knowledge 只读版
  -> Phase 4 文件记忆
  -> Phase 5 skill
  -> Phase 6 TaskTree
  -> Phase 7b World Facts / Semantic Memory 集成
```

原因是 Phase 7 分成两半：

```text
Static Knowledge:
  minecraft-data 的配方、方块、工具等级、掉落物
  可以很早做，不依赖记忆系统

World Facts:
  基地坐标、箱子库存、矿点、危险区域
  依赖 Phase 3/4 的 MemoryLayer
```

因此：

- Phase 7a 可以提前，尤其在 Phase 2 完成后就能提升合成和采集可靠性。
- Phase 7b 应放在 Phase 4 之后，因为它需要 MemoryLayer 和文件型记忆承接运行时世界事实。

## Phase 1：LLM 层迁移

核心提升：

```text
bot 的“脑子”开始可控。
```

能力变化：

- Python 侧统一接管 LLM 调用。
- tool call 解析更稳定。
- history / prompt 构建可测试、可替换。
- provider 接口统一，后续模型切换成本降低。

实际例子：

```text
玩家：去砍 10 个橡木

Phase 1 后：
bot 能稳定解析意图，并生成类似：
!collectBlocks(oak_log, 10)
```

评估指标：

- tool call 解析成功率
- 无效命令率
- 多 provider mock 测试通过率
- history 压缩后是否仍能保持任务上下文

## Phase 2：Agent 核心

核心提升：

```text
bot 从“会聊天”变成“能行动”。
```

能力变化：

- 建立 observe -> plan -> execute -> feedback 主循环。
- ActionExecutor 能把 LLM tool call 转为 Mineflayer 操作。
- BotState / Inventory / Position 等核心状态结构化。
- 对话、模式和执行反馈开始闭环。

实际例子：

```text
玩家：做一把石镐

Phase 2 后：
bot 检查背包 -> 找木头/石头 -> 采集 -> 合成木棍 -> 合成石镐 -> 汇报完成。
```

评估指标：

- 简单任务端到端成功率
- 执行失败后的反馈是否进入下一轮规划
- BotState 是否和真实世界状态一致
- ActionExecutor 是否正确处理中断、失败和超时

## Phase 3：MemoryLayer 契约

核心提升：

```text
所有长期学习有统一入口。
```

短期看，Phase 3 不一定让 bot 立刻“变聪明”，但它能防止后续形成多套互相污染的存储。

能力变化：

- 统一 memory / skill / task 写入入口。
- 定义 MemoryRecord / MemoryQuery / MemoryResult。
- 通过 MemoryPolicy 判断写入、晋升、遗忘和短期保留。
- 为 Phase 4/5/6/7b 打基础。

实际例子：

```text
bot 夜间挖矿死亡。

Phase 3 后：
系统不会让 LLM 直接写“永远不要挖矿”。
事件会进入 MemoryPolicy：
  - 是临时失败？
  - 是 feedback 候选？
  - 是否需要玩家确认？
  - 是否只保留在 episodic memory？
```

评估指标：

- 写入是否都经过 MemoryService / MemoryPolicy
- memory namespace 是否清晰
- 高风险规则是否不会自动进入长期记忆
- 后端是否可替换，不依赖具体文件路径

## Phase 7a：Static Knowledge 只读版

核心提升：

```text
bot 不再靠 LLM 猜配方、工具等级和采集路径。
```

Phase 7a 可在 Phase 3 light 后提前做。它只读取 `minecraft-data`，不写 MemoryLayer。

能力变化：

- 查询 recipe。
- 查询 tool requirement。
- 查询 item/block/entity 基础属性。
- 简单计算 missing materials。
- 合成/采集前进行结构化校验。

实际例子：

```text
玩家：做附魔台

Phase 7a 后：
bot 查询到 enchanting_table 需要：
  - book x1
  - diamond x2
  - obsidian x4

并知道 obsidian 需要 diamond_pickaxe 才能挖。
```

评估指标：

- recipe 查询准确率
- tool requirement 查询准确率
- LLM 配方幻觉率下降
- ActionExecutor 是否能在执行前发现工具不满足

## Phase 4：文件型记忆

核心提升：

```text
bot 开始记得这个世界。
```

能力变化：

- `MEMORY.md` 作为索引始终加载。
- 详情记忆按需读取。
- 支持 places / feedback / facts。
- 玩家可以用短命令标记记忆。

实际例子：

```text
玩家：remember base

Phase 4 后：
bot 写入基地坐标。
之后玩家说“回基地”，bot 能 recall base_location 并导航回去。
```

另一个例子：

```text
玩家：remember this chest as food

bot 记录箱子坐标、维度、附近标志物和用途。
之后需要食物时，bot 可以优先去 food chest。
```

评估指标：

- remember / recall / forget 是否可用
- `MEMORY.md` 是否保持短索引
- 详情文件是否按需加载
- 记忆是否包含 evidence / confidence / valid_when / invalid_when

## Phase 5：文件型 skill

核心提升：

```text
bot 从“每次重新想”变成“复用成功经验”。
```

这是长期行为能力提升最大的阶段之一。

能力变化：

- skill 作为独立 `.md` 文件存储。
- 触发词 + 状态条件匹配 skill。
- 高置信 skill 可直接执行。
- 失败模式可触发 sub-skill。
- success_count / fail_count / confidence 自动更新。

实际例子：

```text
第一次挖钻石：
LLM 需要规划检查工具、准备火把、下矿、避岩浆、挖矿。

保存 mine_diamonds.md 后：
bot 下次只需匹配 skill：
  - 检查铁镐或更好工具
  - 检查火把
  - 检查食物
  - 按稳定流程执行
```

收益：

- 成功率提升
- token 成本下降
- 行为更稳定
- 可人工编辑和 git diff

评估指标：

- skill 匹配准确率
- 高置信 skill 直接执行成功率
- skill 失败后 fallback 是否合理
- LLM 调用次数是否下降
- skill 文件是否保持可读、可审计

## Phase 6：TaskTree Lite

核心提升：

```text
复杂任务可以复盘，并逐步沉淀为可复用模板。
```

Phase 6 不应新增强执行系统，而是提供任务复盘和 skill 晋升素材。

能力变化：

- 记录复杂任务的目标、子任务、依赖、执行结果。
- 保存 TaskRun / task summary。
- learned_rules 交给 MemoryPolicy 判断。
- 成功多次的 task subtree 可晋升为 candidate skill。

实际例子：

```text
玩家：建一个安全基地

Phase 6 后：
bot 记录任务树：
  - 找位置
  - 清场
  - 建墙
  - 放床
  - 放箱子
  - 记录坐标

多次成功后，其中“建围墙”或“放置基础营地设施”可以晋升成 skill。
```

评估指标：

- task summary 是否准确
- learned_rules 是否不会直接污染 skill
- candidate skill 晋升是否有阈值
- 复杂任务复盘是否能帮助下一次规划

## Phase 7b：World Facts / Semantic Memory 集成

核心提升：

```text
bot 能把具体世界里的事实和静态知识结合起来。
```

Phase 7b 应在 Phase 4 后做，因为它需要 MemoryLayer 写入世界事实。

能力变化：

- 运行时写入 `LOCATED_AT` / `STORED_IN` / `DANGEROUS_NEAR` 等事实。
- 世界事实带 ttl / confidence / evidence / world_id。
- Static Knowledge 负责“游戏规则”，World Facts 负责“这个世界里发生了什么”。

实际例子：

```text
Static Knowledge:
obsidian requires diamond_pickaxe

World Fact:
base chest contains 12 obsidian at (-120, 64, 40)

玩家：做附魔台

Phase 7b 后：
bot 知道需要 obsidian，也知道基地箱子里已经有 obsidian，
所以不用重新去挖，可以先回基地取。
```

评估指标：

- 世界事实写入是否经过 MemoryPolicy
- 过期事实是否会降置信或忘记
- 静态知识和世界事实冲突时是否静态知识优先
- bot 是否能利用库存/坐标事实减少无效采集

## 哪个阶段提升最大

按不同维度判断：

```text
从 0 到能用：Phase 2 最大
看起来更聪明：Phase 7a 很大
长期越用越强：Phase 5 最大
世界连续性：Phase 4 最大
复杂任务沉淀：Phase 6 最大
```

整体排序建议：

1. Phase 2 是基础，没有 AgentLoop 和 ActionExecutor，其他阶段都只是材料。
2. Phase 7a 是高性价比增强，能快速降低配方和工具链幻觉。
3. Phase 4 让 bot 记住世界。
4. Phase 5 让 bot 复用成功经验，是长期能力提升关键。
5. Phase 6 让复杂任务可复盘，并为 skill 晋升提供素材。
6. Phase 7b 把静态知识和世界事实打通。

## 推荐验收任务集

### 基础行动任务

```text
collect 10 oak_log
craft stone_pickaxe
go to base
```

主要验证 Phase 1/2。

### 静态知识任务

```text
craft enchanting_table
mine obsidian
craft shield
```

主要验证 Phase 7a。

### 世界记忆任务

```text
remember base
remember this chest as food
go to food chest
```

主要验证 Phase 4。

### Skill 复用任务

```text
save skill safe tunnel
mine diamonds again
recover from lava detected
```

主要验证 Phase 5。

### 复杂任务复盘

```text
build safe starter base
repeat build starter base in another location
promote successful subtree to candidate skill
```

主要验证 Phase 6。

### 静态知识 + 世界事实组合

```text
craft enchanting_table using stored materials
prepare for nether trip from base supplies
```

主要验证 Phase 7b。
