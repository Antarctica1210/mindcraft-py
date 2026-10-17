# "Craft a Bed" 任务执行流程分析

**日期**: 2026-05-11 | **Bot**: lmbot | **模型**: qwen/qwen3.6-35b-a3b | **耗时**: ~4 分钟 | **迭代次数**: 19 次 | **结果**: 成功

---

## 架构概要

JS bot 的任务分解是**涌现式**的，不是预先规划的。核心机制：

```
for (let i = 0; i < max_responses; i++) {
    1. 将完整对话历史发送给 LLM（system prompt + 所有历史回合）
    2. LLM 根据当前上下文决定 下一步 做什么
    3. 解析 LLM 返回的命令并执行
    4. 执行结果追加到对话历史
    5. 回到步骤 1，直到 LLM 无命令返回或结果为空
}
```

每次 LLM 调用只规划**一步**，多步分解通过反复循环涌现。

---

## System Prompt 结构（每次 LLM 调用）

```
[角色定义] You are an AI Minecraft bot named lmbot...
[记忆] Summarized memory:''
[状态] STATS — 位置、血量、饥饿度、天气、时间、模式
[环境] NEARBY_ENTITIES — 附近实体
[环境] NEARBY_BLOCKS — 附近方块及脚下/腿部/头部方块
[库存] INVENTORY — 当前物品列表
[命令] COMMAND DOCS — 20+ 可用命令的参数和描述
[对话] Conversation Begin: — 完整历史回合
```

状态/环境/库存信息在每次 LLM 调用前实时刷新，确保 LLM 看到最新世界状态。

---

## 逐步执行流程

### 请求 #4: Wilson 说 "craft a bed"

**输入上下文**: 之前的对话包含 "come to me" → bot 移动到 Wilson 身边

---

#### Iter 0 — 首次尝试收集羊毛（参数错误）

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I need wool for that. !collectBlocks('sheep', 8)` |
| 检测命令 | `!collectBlocks` |
| 执行结果 | `Command !collectBlocks was given 0 args, but requires 2 args.` |
| 原因 | LLM 用了单引号 `'sheep'` 而非双引号，命令解析器未识别参数 |
| 循环决策 | 结果非空 → **CONTINUE** 下一轮 |

#### Iter 1 — 自纠正：搜索羊

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I don't have wool, so I can't craft a bed yet. I need to find sheep first. !searchForEntity("sheep", 64)` |
| 检测命令 | `!searchForEntity` |
| 执行结果 | 找到羊（37 格远），寻路到达 `(17, 63, 49)` |
| 循环决策 | 结果非空 → **CONTINUE** |

#### Iter 2 — 攻击羊（参数错误）

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I found some sheep nearby. I'll need to kill them to get the wool. !attack("sheep", 3)` |
| 检测命令 | `!attack` |
| 执行结果 | `Command !attack was given 2 args, but requires 1 args.` |
| 循环决策 | 结果非空 → **CONTINUE** |

#### Iter 3 — 自纠正：正确攻击

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I'll attack the sheep to get wool. !attack("sheep")` |
| 检测命令 | `!attack` |
| 执行结果 | 击杀成功，拾取 1 个物品（white_wool + mutton） |
| 循环决策 | 结果非空 → **CONTINUE** |

#### Iter 4 — 尝试再杀一只（附近无羊）

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I got 1 white_wool. I need 3 for a bed. !attack("sheep")` |
| 执行结果 | `Could not find any sheep to attack.` |
| 循环决策 | 结果非空 → **CONTINUE** |

#### Iter 5 — 搜索新羊群

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I need 2 more wool. I'll keep looking for sheep. !searchForEntity("sheep", 64)` |
| 执行结果 | 找到羊（43 格远），寻路到达 |
| 循环决策 | → **CONTINUE** |

#### Iter 6 — 再杀（零掉落）

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I found another sheep. !attack("sheep")` |
| 执行结果 | 击杀成功，但 `Picked up 0 items` |
| 循环决策 | → **CONTINUE** |

#### Iter 7 — 继续搜索

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `That sheep didn't drop wool. I still need 2 more. !searchForEntity("sheep", 64)` |
| 执行结果 | 找到羊（6.9 格远），寻路到达 |
| 循环决策 | → **CONTINUE** |

#### Iter 8 — 第三次杀羊（零掉落）

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I found a sheep very close by. !attack("sheep")` |
| 执行结果 | 击杀成功，但又是 `Picked up 0 items` |
| 循环决策 | → **CONTINUE** |

#### Iter 9 — 检查库存（关键转折）

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `No wool again? That's weird. I'll check my inventory to see what I have. !inventory` |
| 执行结果 | `INVENTORY: mutton 3, oak_log 3, white_wool 3` |
| 关键发现 | **LLM 不知道前期杀羊已经攒够了 3 个羊毛**，检查库存后才发现够了 |
| 循环决策 | → **CONTINUE** |

> **洞察**: 前几只羊虽然显示 "Picked up 0 items"，但实际上羊毛已被拾取。LLM 通过 `!inventory` 命令发现了真实库存状态，这是一个涌现的"纠错"行为。

#### Iter 10 — 合成木板

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I have 3 white_wool. I need 3 total for a bed. I have enough wool! Now I need planks and sticks. !craftRecipe("oak_plank", 3)` |
| 执行结果 | `Successfully crafted oak_planks, you now have 8 oak_planks.` |
| 循环决策 | → **CONTINUE** |

#### Iter 11 — 合成木棍

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I have 8 oak_planks. I need 3 planks and 2 sticks for the bed. !craftRecipe("stick", 2)` |
| 执行结果 | `Successfully crafted stick, you now have 8 stick.` |
| 循环决策 | → **CONTINUE** |

#### Iter 12 — 合成床（物品名错误）

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I have 8 planks and 8 sticks. I have enough to craft the bed. !craftRecipe("bed", 1)` |
| 执行结果 | `Invalid item type: bed.` |
| 循环决策 | 结果非空 → **CONTINUE** |

#### Iter 13 — 自纠正：指定颜色（缺工作台）

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `Oops, I need to specify the color. !craftRecipe("white_bed", 1)` |
| 执行结果 | `Crafting white_bed requires a crafting table.` |
| 循环决策 | → **CONTINUE** |

#### Iter 14 — 合成工作台

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I need a crafting table. !craftRecipe("crafting_table", 1)` |
| 执行结果 | `Successfully crafted crafting_table, you now have 1 crafting_table.` |
| 循环决策 | → **CONTINUE** |

#### Iter 15 — 成功合成床

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I have the crafting table. Now I can craft the bed. !craftRecipe("white_bed", 1)` |
| 执行结果 | `Placed crafting_table at (32, 63, 39). Successfully crafted white_bed, you now have 1 white_bed.` |
| 循环决策 | → **CONTINUE** |

#### Iter 16 — 交付给玩家

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `I've crafted the white bed! !givePlayer("Wilson", "white_bed", 1)` |
| 执行结果 | 寻路到 Wilson，丢弃 white_bed → `Wilson received white_bed.` |
| 循环决策 | → **CONTINUE** |

#### Iter 17 — 结束目标

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `Bed delivered to Wilson! !endGoal` |
| 执行结果 | `Self-prompting stopped.` |
| 循环决策 | → **CONTINUE** |

#### Iter 18 — 静默退出

| 阶段 | 内容 |
|------|------|
| LLM 响应 | `!stfu` |
| 执行结果 | 空 |
| 循环决策 | 结果为空 → **BREAK**，循环结束 |

---

## 关键观察

### 1. 涌现式纠错

LLM 出现了 3 次参数错误，每次都通过下一次迭代自纠正：
- `!collectBlocks('sheep', 8)` → 单引号导致 0 args → 改用 `!searchForEntity`
- `!attack("sheep", 3)` → 多余参数 → 去掉参数
- `!craftRecipe("bed", 1)` → 错误的物品名 → 改用 `"white_bed"`

**机制**: 错误信息被追加到对话历史，LLM 下一轮看到错误后调整策略。

### 2. 库存感知差距

LLM 在 Iter 4 声称只有 1 个羊毛（实际已有 3 个），因为之前的执行结果只说了 "Picked up 1 items" 没说具体捡到了什么。这导致了多余的杀羊尝试。直到 Iter 9 通过 `!inventory` 才获知真相。

### 3. 逐步材料规划

LLM 没有一开始就列出完整计划（羊毛→木板→木棍→工作台→床），而是每完成一种材料后才决定下一种：
- 确认羊毛够 → 合成木板
- 合成木板后 → 合成木棍  
- 合成木棍后 → 合成床

### 4. 历史自动压缩

观察各轮次的对话历史长度（11→13→9→11→9→11→9→13→9 条），系统自动裁剪了过长的历史，保留了近期的上下文。

### 5. 19 次迭代的效率

整个 "craft a bed" 用了 19 次 LLM 调用，其中：
- **11 次**用于获取羊毛（包括 3 次参数纠错、2 次无效杀羊）
- **6 次**用于合成链（木板→木棍→床→纠错→工作台→床）
- **2 次**用于交付和清理

主要时间花在收集羊毛的试错上，合成链相对高效。

---

## LLM 输入/输出逐轮分析

以下从 conversation 日志中提取每次 LLM 调用的完整输入和输出，分析 LLM 的决策逻辑。

### 数据说明

每轮 LLM 调用的**输入**由两部分组成：

| 组成部分 | 来源 | 刷新频率 |
|----------|------|----------|
| System Prompt | `profiles/defaults/_default.json` + 实时变量替换 | 每次调用前刷新状态/库存/环境 |
| Messages | 裁剪后的对话历史 | 每次执行后追加 |

---

### Iter 0 — 首次响应 "craft a bed"

**输入 — System Prompt 关键字段**:

```
INVENTORY: oak_log: 3     ← 初始状态，只有 3 根原木

NEARBY_BLOCKS:
  - grass_block, short_grass, dirt, stone, cornflower, copper_ore, coal_ore, gravel
  - Block Below: grass_block

NEARBY_ENTITIES: Human player: Wilson
```

**输入 — Messages（8 turns）**:

```
[0] system: Respond with hello world and your name
[1] user:   Wilson: come to me
[2] assistant: My brain disconnected, try again.
[3] user:   Wilson: come to me
[4] assistant: On my way! !goToPlayer("Wilson", 3)
[5] system: Found non-destructive path. You have reached Wilson.
[6] assistant: Here I am!
[7] user:   Wilson: craft a bed                     ← 用户的指令
```

**LLM 输出**:

```
I need wool for that. !collectBlocks('sheep', 8)
```

**决策分析**: LLM 知道床需要羊毛，但没有 wool。它试图用 `!collectBlocks` 收集"sheep"，犯了两个错误：① 收集的对象应该是 block 而非实体，② 使用了单引号导致参数解析失败（命令收到 0 args）。附近的 sheep 实体没有出现在 NEARBY_ENTITIES 中（只在迭代中期才出现），因此 LLM 无法通过 `!searchForEntity` 定位。

**执行结果**: `Command !collectBlocks was given 0 args, but requires 2 args.`

---

### Iter 1 — 自纠正为搜索实体

**输入 — Messages 追加了**:

```
[8] assistant: I need wool for that. !collectBlocks
[9] system:   Command !collectBlocks was given 0 args, but requires 2 args.
```

**LLM 输出**:

```
I don't have wool, so I can't craft a bed yet. I need to find sheep first. !searchForEntity("sheep", 64)
```

**决策分析**: LLM 看到 `!collectBlocks` 的错误反馈，意识到需要换一种方式获取羊毛。它从可用命令中选择了 `!searchForEntity`（参数正确使用双引号），表现出**从错误中学习**的能力。

---

### Iter 2-8 — 杀羊循环（参数纠错 + 零掉落困惑）

这 7 轮是反复的"搜索羊 → 攻击 → 检查掉落"循环，核心的输入/输出模式如下：

| Iter | LLM 输入（上一轮的反馈） | LLM 输出 | 分析 |
|------|--------------------------|----------|------|
| 2 | `Found sheep 37 blocks away` | `!attack("sheep", 3)` | 多余参数 3，命令只接受 1 个参数 |
| 3 | `!attack was given 2 args, but requires 1` | `!attack("sheep")` | 自纠正，去掉参数 |
| 3 exec | — | 击杀成功 + `Picked up 1 items` | LLM 不知道捡到了什么 |
| 4 | 知道自己有 1 wool，还需 2 个 | `!attack("sheep")` | 尝试再杀，但附近无羊 |
| 4 exec | `Could not find any sheep to attack` | — | 要重新搜索 |
| 5 | 需 2 more wool | `!searchForEntity("sheep", 64)` | 再找羊 |
| 6 | 找到羊了 | `!attack("sheep")` | 击杀但 0 掉落 |
| 7 | 0 掉落，需 2 more | `!searchForEntity("sheep", 64)` | 继续找 |
| 8 | 找到羊了 | `!attack("sheep")` | 击杀但 0 掉落 |

**关键观察**: Iter 4 时 LLM 以为只有 1 个羊毛，实际 Iter 3 的 "Picked up 1 items" 返回了 white_wool。LLM 没有收到明确的物品名，导致低估了库存。

---

### Iter 9 — 库存检查（转折点）

**输入 — Messages（最后部分）**:

```
[n] assistant: I found a sheep very close by. !attack("sheep")
[n+1] system:  Successfully killed sheep. Picked up 0 items.
```

连续 3 只羊 0 掉落，LLM 感到困惑。

**LLM 输出**:

```
No wool again? That's weird. I'll check my inventory to see what I have. !inventory
```

**决策分析**: LLM 展现出**元认知行为** — 它意识到自己对库存的认知可能不准确，主动调用 `!inventory` 获取真实状态。这是一个涌现出的"验证假设"行为。

**执行结果**:

```
INVENTORY
- mutton: 3
- oak_log: 3
- white_wool: 3          ← LLM 之前以为自己只有 1 个！
WEARING: Nothing
```

**关键转折**: 库存检查揭示了真相 — 前面看起来 "Picked up 0 items" 的击杀实际上累积了 3 个羊毛（可能是羊毛掉落后被自动拾取，但命令返回值没说清楚）。

**输入 — System Prompt 库存字段的变化**:

```
# Iter 0-8（杀羊阶段）
INVENTORY: oak_log: 3

# Iter 9 !inventory 之后的轮次
INVENTORY: mutton: 3, oak_log: 3, white_wool: 3    ← 终于正确
```

---

### Iter 10 — 合成木板

**输入 — Messages 最后部分**:

```
[n] assistant: No wool again? That's weird. I'll check my inventory... !inventory
[n+1] system:  INVENTORY
                - mutton: 3
                - oak_log: 3
                - white_wool: 3
```

**输入 — System Prompt 关键字段**:

```
INVENTORY
- oak_log: 3              ← 有原木，可以加工
- mutton: 3
- white_wool: 3           ← 羊毛够了

Summarized memory: '...Status: Has 1 white_wool. Need 2 more...'  ← 记忆已过时！
```

> 注意：总结记忆仍说只有 1 个羊毛（杀第一只羊时的快照），但 LLM 相信了 inventory 命令的实时输出，没有被过时记忆误导。

**LLM 输出**:

```
I have 3 white_wool. I need 3 total for a bed. I have enough wool! Now I need planks and sticks. !craftRecipe("oak_plank", 3)
```

**决策分析**: LLM 完成了第一个子目标的确认（羊毛 ✓），并制定了接下来的子目标（planks + sticks）。它知道 `oak_log → oak_planks` 的转换，但**没有调用 `!getCraftingPlan` 来检查床的完整配方树**。

**执行结果**: `Successfully crafted oak_planks, you now have 8 oak_planks.`

---

### Iter 11 — 合成木棍

**输入 — Messages 追加了**:

```
[n] assistant: I have 3 white_wool... Now I need planks and sticks. !craftRecipe("oak_plank", 3)
[n+1] system:  Successfully crafted oak_planks, you now have 8 oak_planks.
```

**输入 — System Prompt 库存**:

```
INVENTORY
- oak_planks: 12          ← 包括了之前已有的 4 个？
- mutton: 3
- white_wool: 3
```

**LLM 输出**:

```
I have 8 oak_planks. I need 3 planks and 2 sticks for the bed. !craftRecipe("stick", 2)
```

**决策分析**: LLM 按之前制定的物料清单执行：planks ✓ → 下一步 sticks。它认为自己完成了 2/3 的材料准备。

**执行结果**: `Successfully crafted stick, you now have 8 stick.`

---

### Iter 12 — 尝试合成床（意外失败）

**输入 — Messages 最后部分**:

```
[n] assistant: I have 8 oak_planks. I need 3 planks and 2 sticks for the bed. !craftRecipe("stick", 2)
[n+1] system:  Successfully crafted stick, you now have 8 stick.
```

**输入 — System Prompt 库存**:

```
INVENTORY
- oak_planks: 8
- stick: 8
- mutton: 3
- white_wool: 3           ← 三个材料都齐了
```

**LLM 输出**:

```
I have 8 planks and 8 sticks. I have enough to craft the bed. !craftRecipe("bed", 1)
```

**决策分析**: 从 LLM 的视角，它的物料清单（wool ✓ → planks ✓ → sticks ✓）全部完成。它认为 `craftRecipe("bed", 1)` 是合理的下一步。这里暴露了**系统提示信息缺失**的问题 —— 没有任何地方告诉 LLM：
1. 床的物品 ID 是 `white_bed` 而非 `bed`
2. `white_bed` 需要 crafting table 作为前置

**执行结果**: `Invalid item type: bed.` ← **第一个意外**

---

### Iter 13 — 修正物品名 + 发现工作台需求

**输入 — Messages 追加了**:

```
[n] assistant: I have 8 planks and 8 sticks. I have enough to craft the bed. !craftRecipe("bed", 1)
[n+1] system:  Invalid item type: bed.     ← 错误反馈
```

**LLM 输出**:

```
Oops, I need to specify the color. !craftRecipe("white_bed", 1)
```

**决策分析**: LLM 将错误理解为 "bed 可能叫 white_bed"。它根据 Minecraft 知识修正了物品名，但仍**不知道需要工作台**。

**执行结果**: `Crafting white_bed requires a crafting table.` ← **第二个意外**

---

### Iter 14 — 合成工作台

**输入 — Messages 追加了**:

```
[n] assistant: Oops, I need to specify the color. !craftRecipe("white_bed", 1)
[n+1] system:  Crafting white_bed requires a crafting table.   ← 终于知道约束
```

**LLM 输出**:

```
I need a crafting table. !craftRecipe("crafting_table", 1)
```

**决策分析**: LLM 通过错误反馈**终于**知道了需要 crafting table。它立即调用 craftRecipe 合成。这表明 LLM 具备响应式问题解决能力，但它**无法在第一次就做对**，因为系统提示中没有这个约束信息。

**执行结果**: `Successfully crafted crafting_table, you now have 1 crafting_table.`

---

### Iter 15 — 成功合成 + Iter 16 — 交付

**输入 — Messages 追加了**:

```
[n] assistant: I need a crafting table. !craftRecipe("crafting_table", 1)
[n+1] system:  Successfully crafted crafting_table, you now have 1 crafting_table.
```

**LLM 输出**:

```
# Iter 15
I have the crafting table. Now I can craft the bed. !craftRecipe("white_bed", 1)

# Iter 16（看到成功结果后）
I've crafted the white bed! !givePlayer("Wilson", "white_bed", 1)
```

**决策分析**: 工作台条件满足后，LLM 重试 `!craftRecipe("white_bed", 1)` 并成功。然后自动执行交付逻辑 — 把成品交给请求者 Wilson。

**Iter 15 执行结果**: `Placed crafting_table at (32, 63, 39). Successfully crafted white_bed, you now have 1 white_bed.`
**Iter 16 执行结果**: `Found non-destructive path. You have reached Wilson. Wilson received white_bed.`

---

### 输入/输出模式总结

```
┌─────────────────────────────────────────────────────────────────┐
│  LLM 决策模式                                                    │
├─────────────────────────────────────────────────────────────────┤
│  1. 接收 System Prompt（实时状态 + 命令文档 + 对话历史）         │
│  2. 检查当前状态 vs. 目标                                        │
│  3. 选择一个命令执行下一步                                       │
│  4. 命令结果追加到历史 → 下次调用看到更新后的世界                │
│                                                                  │
│  关键特征:                                                       │
│  • 每轮只做一步 — LLM 不知道自己的"下一步"会是什么              │
│  • 自纠错 — 错误反馈 → 修正策略                                  │
│  • 信息缺口 — 命令文档缺失的约束只能在失败后获得                 │
│  • 元认知 — 不确定时主动检查（!inventory）                      │
└─────────────────────────────────────────────────────────────────┘
```

| 问题 | 原因 | 影响 | 修复 |
|------|------|------|------|
| 跳过工作台 | `!craftRecipe` 文档未提 crafting table 要求 | +3 轮无效迭代 | 方案 A/B |
| 物品名错误 | `!craftRecipe` 文档未说明物品命名约定 | +1 轮 | 方案 A |
| 库存误判 | 命令返回值 "Picked up N items" 未列出具体物品 | +5 轮无效杀羊 | `!inventory` 自愈 |
| 记忆滞后 | 总结记忆是批量写入的快照 | LLM 忽略了它（幸好） | 方案 C |
| 参数错误 | LLM 猜测参数格式 | +3 轮 | 涌现式自纠 |

---

## 问题分析：为什么跳过工作台直接合成床

### 根因

LLM 在 Iter 12 尝试 `!craftRecipe("bed", 1)` 直接合成床，跳过了工作台（crafting table）。这不是 LLM 缺乏 Minecraft 知识，而是**系统提示缺少关键约束信息**，导致 LLM 必须在试错中逐步发现前置条件。

### 具体原因

#### 1. `!craftRecipe` 命令文档没有提到工作台要求

当前系统提示中 `!craftRecipe` 的描述：

```
!craftRecipe: Craft the given recipe a given number of times.
Params:
  recipe_name: (string) The name of the output item to craft.
  num: (number) The number of times to craft the recipe.
```

没有任何提示告知某些配方需要 crafting table。LLM 唯一的获知途径是等命令失败后的错误反馈。

#### 2. LLM 遵循自己列出的"物料清单"

| Iter | LLM 的认知 | 实际库存 |
|------|-----------|----------|
| 10 | "需要 planks 和 sticks" | wool 3, log 3 |
| 11 | "planks ✓ → 还需要 sticks" | wool 3, planks 8 |
| 12 | "wool + planks + sticks → 直接合成床" | wool 3, planks 8, sticks 8 |

LLM 理解的配方是 `羊毛 + 木板 + 木棍 → 床`，不知道中间有 crafting table 依赖。

#### 3. 两个可预防的命令未被使用

| 命令 | 描述 | 此任务中 |
|------|------|----------|
| `!craftable` | 列出当前库存可合成的所有物品 | **未使用** |
| `!getCraftingPlan` | 显示完整原料树（包含中间产物）| **从未使用过** |

如果 LLM 在拿到羊毛后调用 `!getCraftingPlan("white_bed")`，会提前知道需要 crafting table。

#### 4. 总结记忆滞后

```
Summarized memory: '...Status: Has 1 white_wool. Need 2 more...'
```

总结记忆停留在杀第一只羊的状态，实际库存已有 3 个羊毛。虽然本次未直接导致工作台问题，但反映了记忆系统和实时库存之间的不同步。

### 事实上的"试错纠错"

LLM 通过 3 轮迭代才到达正确路径：

```
Iter 12: !craftRecipe("bed", 1)           → Invalid item type: bed
Iter 13: !craftRecipe("white_bed", 1)     → requires crafting table   ← 才知道
Iter 14: !craftRecipe("crafting_table", 1) → 成功
Iter 15: !craftRecipe("white_bed", 1)     → 成功交付
```

---

## 修复建议

### 方案 A：增强 `!craftRecipe` 命令文档（低成本）

在命令文档中加入工作台约束提示：

```
!craftRecipe: Craft the given recipe a given number of times.
  Note: Some recipes (beds, tools, armor, etc.) require a placed crafting table nearby.
  Use !getCraftingPlan first to check requirements.
Params:
  recipe_name: (string) The name of the output item to craft.
  num: (number) The number of times to craft the recipe.
```

**预期效果**: LLM 在下达 craftRecipe 前会意识到需要确认前置条件，减少无效尝试。

### 方案 B：在角色提示中加入规划引导（中等成本）

在系统提示的角色定义部分加入：

```
IMPORTANT — Crafting and multi-step tasks:
  - Before crafting any complex item, use !getCraftingPlan to see the full ingredient tree.
  - Some recipes require a placed crafting table — check before you craft.
  - After each step, check the result and decide the next action. Do not skip steps.
```

**预期效果**: LLM 在处理合成任务时会优先调用 `!getCraftingPlan`，从源头避免跳过中间产物。

### 方案 C：修复总结记忆与库存的同步（长期优化）

当前总结记忆滞后于实时库存状态。理想情况下，`$MEMORY` 变量应该反映最新的库存摘要，而非批量写入时的快照。这可以减少 LLM 对过时信息的依赖。

### 优先级建议

| 方案 | 成本 | 影响范围 | 优先级 |
|------|------|----------|--------|
| A — 增强命令文档 | 低（改一行描述） | 所有合成类任务 | **立即** |
| B — 角色规划引导 | 中（改系统提示模板） | 所有多步骤任务 | **立即** |
| C — 记忆同步优化 | 高（改记忆写入逻辑） | 全局 | 后续迭代 |

---

## LLM 输入/输出数据结构（JS → 请求 / 响应 → 命令）

### 整体数据流

```
┌──────────┐    ┌─────────────────┐    ┌──────────────────┐    ┌────────────────┐
│ History  │───▶│ replaceStrings() │───▶│ strictFormat()   │───▶│ OpenAI API     │
│ turns[]  │    │ system prompt    │    │ role remap       │    │ chat.complet.. │
│ {role,   │    │ $NAME, $STATS,   │    │ merge, fillers   │    │ .create(req)   │
│  content}│    │ $INVENTORY, ...  │    │                  │    │                │
└──────────┘    └─────────────────┘    └──────────────────┘    └───────┬────────┘
                                                                        │
                                                          ┌─────────────┘
                                                          ▼
┌──────────────┐    ┌──────────────────┐    ┌──────────────────────────┐
│ executeCmd() │◀───│ containsCommand()│◀───│ choices[0].message       │
│ 执行命令     │    │ regex parse      │    │ .content (plain text)    │
└──────────────┘    └──────────────────┘    └──────────────────────────┘
```

### 1. 对话历史数据结构 (History turns)

存储在 `this.history.getHistory()` 中，每条记录的格式：

```typescript
// TypeScript-like definition
type Turn = {
    role: "user" | "assistant" | "system",
    content: string
};
```

**实际数据示例** — Iter 0 "craft a bed" 的 8 轮对话历史：

```json
[
  {"role": "user",     "content": "SYSTEM: Respond with hello world and your name"},
  {"role": "assistant", "content": "Hello world, I'm lmbot."},
  {"role": "user",     "content": "Wilson: come to me"},
  {"role": "assistant", "content": "My brain disconnected, try again."},
  {"role": "user",     "content": "Wilson: come to me"},
  {"role": "assistant", "content": "On my way! !goToPlayer(\"Wilson\", 3)"},
  {"role": "user",     "content": "SYSTEM: Action output:\nFound non-destructive path.\nYou have reached Wilson."},
  {"role": "assistant", "content": "Here I am!"},
  {"role": "user",     "content": "Wilson: craft a bed"}
]
```

**角色含义**:

| role | 含义 | 内容格式 |
|------|------|----------|
| `user` | 外部输入 | `"Wilson: <message>"` 或 `"SYSTEM: <result>"` 或 `"<bot_name>: <message>"` |
| `assistant` | bot 自己的输出 | `"<自然语言> !commandName(\"arg\", 123)"` |
| `system` | 内部事件 | behavior log, 死亡通知等 |

### 2. System Prompt 构建 (`replaceStrings()`)

在 `prompter.js:replaceStrings()` 中，模板字符串被实时变量替换：

```
模板: profiles/defaults/_default.json → profile.conversing
替换: $NAME, $STATS, $INVENTORY, $COMMAND_DOCS, $MEMORY, $SELF_PROMPT, $EXAMPLES, ...
```

**完整 System Prompt 结构**（实际发送的字符串）:

```text
You are an AI Minecraft bot named lmbot that can converse with players, see, move, 
mine, build, and interact with the world by using commands.
 Be a friendly, casual, effective, and efficient robot. ...

Summarized memory:'...'                          ← $MEMORY

STATS                                             ← $STATS
- Position: x: 30.52, y: 66.00, z: 84.45
- Gamemode: survival
- Health: 20 / 20
- Hunger: 20 / 20
- Biome: plains
- Weather: Clear
- Time: Night
- Current Action: Idle
- Nearby Human Players: Wilson
- Nearby Bot Players: None.
Agent Modes: ...

NEARBY_ENTITIES                                   ← !entities 命令输出
- Human player: Wilson

NEARBY_BLOCKS                                     ← !nearbyBlocks 命令输出
- grass_block, short_grass, dirt, stone, ...
- Block Below: grass_block
- Block at Legs: air
- Block at Head: air
- First Solid Block Above Head: none

INVENTORY                                         ← !inventory 命令输出
- oak_log: 3
- white_wool: 3
WEARING: Nothing

*COMMAND DOCS                                     ← getCommandDocs()
 You can use the following commands to perform actions...
 !stats: Get your bot's location, health, hunger, and time of day.
 !inventory: Get your bot's inventory.
 !craftRecipe: Craft the given recipe a given number of times.
 ...

Conversation Begin:                               ← 对话历史（实际历史通过 messages 参数传入）
```

### 3. API 请求 — `strictFormat()` 转换

`strictFormat()` 在 `src/utils/text.js:44` 将 history turns 转换为 OpenAI API 兼容格式：

**转换规则**:

| 原始 role | 转换后 role | 内容处理 |
|-----------|-------------|----------|
| `system` | `user` | 前缀 `"SYSTEM: "` |
| `user` | `user` | 保持不变 |
| `assistant` | `assistant` | 保持不变 |
| 连续相同 role | 合并到上一条 | `content += '\n' + newContent` |
| 连续 assistant | 插入 filler | `{role: "user", content: "_"}` 分隔 |
| 首条非 user | prepend filler | `{role: "user", content: "_"}` |

**Iter 0 示例 — 8 轮历史经过 `strictFormat()` 后**:

```json
[
  {"role": "user", "content": "SYSTEM: Respond with hello world and your name"},
  {"role": "assistant", "content": "Hello world, I'm lmbot."},
  {"role": "user", "content": "Wilson: come to me"},
  {"role": "assistant", "content": "My brain disconnected, try again."},
  {"role": "user", "content": "Wilson: come to me"},
  {"role": "assistant", "content": "On my way! !goToPlayer(\"Wilson\", 3)"},
  {"role": "user", "content": "SYSTEM: Action output:\nFound non-destructive path.\nYou have reached Wilson."},
  {"role": "assistant", "content": "Here I am!"},
  {"role": "user", "content": "Wilson: craft a bed"}
]
```

> 本例中所有 role 均交替出现，无合并或 filler 插入。

### 4. 最终 API 请求体

`lmstudio.js:sendRequest()` 构造的实际 HTTP 请求：

```json
{
  "model": "qwen/qwen3.6-35b-a3b",
  "messages": [
    {"role": "system", "content": "You are an AI Minecraft bot named lmbot...\n\nSTATS\n...\n\nINVENTORY\n...\n\n*COMMAND DOCS\n...\n\nConversation Begin:"},
    {"role": "user", "content": "SYSTEM: Respond with hello world and your name"},
    {"role": "assistant", "content": "Hello world, I'm lmbot."},
    {"role": "user", "content": "Wilson: come to me"},
    {"role": "assistant", "content": "My brain disconnected, try again."},
    {"role": "user", "content": "Wilson: come to me"},
    {"role": "assistant", "content": "On my way! !goToPlayer(\"Wilson\", 3)"},
    {"role": "user", "content": "SYSTEM: Action output:\nFound non-destructive path.\nYou have reached Wilson."},
    {"role": "assistant", "content": "Here I am!"},
    {"role": "user", "content": "Wilson: craft a bed"}
  ],
  "stop": ["***"]
}
```

**关键字段说明**:

| 字段 | 值来源 | 说明 |
|------|--------|------|
| `model` | `profile.model` | 模型标识符 |
| `messages[0]` | `systemMessage` 参数 | 完整的替换后 system prompt |
| `messages[1..n]` | `strictFormat(turns)` | 转换后的对话历史 |
| `stop` | 默认 `"***"` | 停止序列（本例未在 prompt 中使用） |

### 5. API 响应 — LLM 输出

LLM 返回的原始文本字符串：

```text
// Iter 0 响应
I need wool for that. !collectBlocks('sheep', 8)

// Iter 1 响应
I don't have wool, so I can't craft a bed yet. I need to find sheep first. !searchForEntity("sheep", 64)

// Iter 9 响应（库存检查 — 元认知行为）
No wool again? That's weird. I'll check my inventory to see what I have. !inventory

// Iter 10 响应（转向合成）
I have 3 white_wool. I need 3 total for a bed. I have enough wool! Now I need planks and sticks. !craftRecipe("oak_plank", 3)

// Iter 15 响应（成功）
I have the crafting table. Now I can craft the bed. !craftRecipe("white_bed", 1)
```

**响应结构**: 纯字符串，格式为 `[可选的解释性文字] !commandName(args)`。

### 6. 命令解析 (`containsCommand()`)

从 LLM 文本响应中提取命令的正则表达式：

```javascript
// src/agent/commands/index.js:29
const commandRegex = /!(\w+)(?:\(((?:-?\d+(?:\.\d+)?|true|false|"[^"]*")(?:\s*,\s*(?:-?\d+(?:\.\d+)?|true|false|"[^"]*"))*)\))?/;

function containsCommand(message) {
    const commandMatch = message.match(commandRegex);
    if (commandMatch) return "!" + commandMatch[1];
    return null;
}
```

**支持的参数类型**:

| 类型 | 正则模式 | 示例 |
|------|----------|------|
| 整数 | `-?\d+` | `64`, `-1`, `3` |
| 浮点数 | `-?\d+(?:\.\d+)?` | `3.0`, `-2.5` |
| 布尔值 | `true\|false` | `true` |
| 字符串 | `"[^"]*"` | `"sheep"`, `"white_bed"` |

**解析示例**:

```javascript
// 输入
"I need wool. !searchForEntity(\"sheep\", 64)"

// containsCommand() → "!searchForEntity"
// 后续 executeCommand() 解析出完整参数: ["sheep", 64]
```

### 7. 执行结果追加到历史

命令执行后，结果作为新 turn 追加：

```javascript
// agent.js handleMessage() 中的逻辑
let execute_res = await executeCommand(this, res);

if (execute_res)
    this.history.add('system', execute_res);   // role=system 的角色
else
    break;  // 空结果 → 结束循环
```

**执行结果格式**:

```text
// 成功
"Action output:\nSuccessfully crafted oak_planks, you now have 8 oak_planks."

// 参数错误
"Command !collectBlocks was given 0 args, but requires 2 args."

// 业务约束
"Action output:\nCrafting white_bed requires a crafting table."

// 未找到
"Action output:\nCould not find any sheep to attack."

// 库存查询
"INVENTORY\n- mutton: 3\n- oak_log: 3\n- white_wool: 3\nWEARING: Nothing"
```

**追加后的 history turn**:

```json
{
  "role": "system",     // 在 strictFormat() 中会被转成 "user" 并加 "SYSTEM: " 前缀
  "content": "Action output:\nSuccessfully crafted oak_planks, you now have 8 oak_planks."
}
```

### 8. Python 重构的差异

JS 项目和 Python 重构的核心数据结构差异：

| 维度 | JS（原始） | Python（重构） |
|------|-----------|---------------|
| **LLM 输出格式** | 纯文本字符串 | 结构化 `tool_calls[]` |
| **命令解析** | Regex `!cmdName(args)` | `tool_choice="required"` 强制 tool call |
| **错误反馈** | `history.add('system', result)` | `history.add` with `role="tool"` + `tool_call_id` |
| **参数类型** | 正则推断（string/number/bool） | JSON Schema 定义在 tool definition 中 |
| **消息格式** | `{role, content}` 纯文本 | `{role, content, tool_calls?, tool_call_id?}` |
| **role 映射** | `strictFormat()` 转换 system→user | OpenAI 原生 `system/user/assistant/tool` |
| **stop 序列** | `stop: "***"` | 无（使用 `tool_choice` 替代） |

**Python 重构中一次完整的请求/响应流程**（以 Iter 1 为例）:

```python
# ── 请求 ──
{
  "model": "...",
  "messages": [
    {"role": "system", "content": "<system prompt>"},
    {"role": "user", "content": "Wilson: craft a bed"},
    # Iter 0 的 tool call 结果
    {"role": "assistant", "content": "I need wool for that.",
     "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "action:collectBlocks",
                                  "arguments": "{\"type\": \"sheep\", \"num\": 8}"}}]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": "Command !collectBlocks was given 0 args, but requires 2 args."}
  ],
  "tools": [
    {"type": "function",
     "function": {"name": "action:searchForEntity",
                  "description": "Find and go to the nearest entity...",
                  "parameters": {"type": "object",
                                 "properties": {
                                   "type": {"type": "string", "description": "..."},
                                   "search_range": {"type": "number", "description": "..."}
                                 },
                                 "required": ["type", "search_range"]}}},
    # ... 其他工具定义
  ],
  "tool_choice": "required"    # 强制返回 tool call，不返回纯文本
}

# ── 响应 ──
{
  "choices": [{
    "message": {
      "content": "I don't have wool, so I can't craft a bed yet. I need to find sheep first.",
      "tool_calls": [{
        "id": "call_2",
        "type": "function",
        "function": {
          "name": "action:searchForEntity",
          "arguments": "{\"type\": \"sheep\", \"search_range\": 64}"
        }
      }]
    }
  }]
}
```

**关键设计决策**:

1. **`tool_choice: "required"`**: 强制 LLM 每次返回 tool call，消除文本解析路径。纯解释性文字放在 `content` 字段中，命令放在 `tool_calls` 中。

2. **`role: "tool"`**: 命令执行结果使用 OpenAI 原生的 tool role，并携带 `tool_call_id` 关联到触发该命令的 assistant 消息。这确保了多轮 tool call 的正确上下文链路。

3. **工具名映射**: JS 的命令名 `!searchForEntity` 在 Python 中映射为 `action:searchForEntity`，通过 `action:` 前缀区分工具调用和普通对话。

4. **参数验证下沉**: JS 中参数类型在 regex 层面推断，Python 中参数由 LLM 根据 JSON Schema 生成 JSON 格式的 arguments，在 executor 层面通过 Pydantic 验证。这消除了 JS 中常见的"参数个数/类型不匹配"错误。

### 9. 保证每步可执行的条件

要让 LLM 每一步都返回一个**可执行的命令**，需要满足：

| 条件 | JS 如何满足 | Python 如何满足 |
|------|------------|----------------|
| **命令列表清晰** | `$COMMAND_DOCS` 列出所有命令及参数 | `tools[]` 定义所有 function 及 JSON Schema |
| **错误可感知** | 失败结果追加为 `role: "system"` 历史 | 失败结果追加为 `role: "tool"` 历史 |
| **状态实时刷新** | 每轮 `replaceStrings()` 重新获取 STATS/INVENTORY | `message_builder.py` 每轮重建 prompt |
| **历史裁剪** | `history.getHistory()` 自动裁剪到 max_turns | `send_request()` 中 context-length 超限时 trim |
| **命令参数明确** | regex 支持 4 种类型 | JSON Schema 类型系统 |
| **单一命令约束** | 角色提示中 "Only use one command in each response" | `tool_choice="required"` 确保一个 tool call |

> **核心保障**: `history.add('system', execute_res)` / `history.add` with `role="tool"` 将每一步执行结果追加到上下文，确保 LLM 始终基于最新世界状态决定下一步。这是涌现式任务分解的基础设施。只要这个闭环存在，LLM 就能通过试错逐步推进任务。
