# P3: 知识图谱 — 世界对象关系

## 动机

LLM 对 Minecraft 的领域知识不完全可靠：可能幻觉合成配方、搞错工具的用途、不知道某种矿石需要什么等级的镐。当前系统完全依赖 LLM 训练数据中的 Minecraft 知识，出错时只能靠试错来修正，效率低。

知识图谱将这些固化的领域知识显式化，减少 LLM 的领域知识负担。

## 设计

```
[oak_log] ──crafts_into──→ [oak_planks] ──crafts_into──→ [crafting_table]
    │                            │
    │──found_in──→ [oak_tree]    │──crafts_into──→ [stick] ──crafts_into──→ [wooden_pickaxe]
    │                            │
    │──mined_with──→ [axe]       │──crafts_into──→ [wooden_door]

[crafting_table] ──located_at──→ (10, 64, 20)
[zombie] ──weak_to──→ [iron_sword]
[furnace] ──requires_fuel──→ [coal, charcoal]
```

## 实体类型

```javascript
// 节点类型
const NodeType = {
  ITEM: "item",           // 物品：oak_log, iron_sword, diamond
  BLOCK: "block",         // 方块：oak_tree, furnace, crafting_table
  ENTITY: "entity",       // 实体：zombie, skeleton, cow
  LOCATION: "location",   // 位置：(x, y, z)
  BIOME: "biome",         // 生物群系：plains, desert
  TOOL: "tool"            // 工具：axe, pickaxe, shovel
};

// 边类型
const EdgeType = {
  CRAFTS_INTO: "crafts_into",       // 合成关系
  SMELTS_INTO: "smelts_into",       // 熔炼关系
  FOUND_IN: "found_in",             // 来源位置
  MINED_WITH: "mined_with",         // 挖掘工具要求
  WEAK_TO: "weak_to",               // 弱点/克制
  LOCATED_AT: "located_at",         // 实例位置
  STORED_IN: "stored_in",           // 存储位置
  REQUIRES_FUEL: "requires_fuel",   // 燃料需求
  DROPS: "drops",                   // 掉落物
  BREW_INTO: "brew_into"            // 酿造关系
};
```

## 数据来源

| 来源 | 内容 | 方式 |
|------|------|------|
| `minecraft-data` 包 | 所有物品/方块/合成配方/实体数据 | 启动时从 npm 包解析，自动建图 |
| 运行时学习 | 动态位置（箱子在哪、熔炉在哪） | agent 执行动作后更新 `LOCATED_AT` / `STORED_IN` 边 |
| 手动编辑 | 战术知识（打僵尸用铁剑） | JSON 配置文件 |

## 查询接口

```javascript
class KnowledgeGraph {
  // 查询合成路径："how to make stone_pickaxe"
  getCraftingPath(targetItem) {
    // BFS 从 targetItem 反向查所有原料
    // 返回树状结构：stone_pickaxe → [stick, cobblestone]
  }

  // 查询采集路径："how to get diamond"
  getAcquisitionPath(item) {
    // 查 found_in → 需要的工具 mined_with
    // diamond → found_in: diamond_ore → mined_with: iron_pickaxe
  }

  // 注入 prompt
  // 在 system prompt 中动态注入与当前任务相关的知识图谱子图
  getRelevantContext(taskDescription) {
    // 1. taskDescription 中提取物品/实体名
    // 2. 从图中提取 2-hop 子图
    // 3. 序列化为自然语言注入 prompt
  }
}
```

## Prompt 注入示例

```
# 注入前 (当前)
You are a Minecraft bot. Use commands to complete tasks.

# 注入后
You are a Minecraft bot. Use commands to complete tasks.

## Relevant Knowledge
- stick is crafted from oak_planks (2:4 ratio)
- stone_pickaxe is crafted from cobblestone (x3) + stick (x2)
- stone_pickaxe can mine iron_ore and below
- iron_ore is found in caves (y < 64)
```

## 关键模块改动

| 模块 | 改动 |
|------|------|
| 新文件 `agent/knowledge_graph.js` | 图构建、查询、序列化 |
| `models/prompter.js` | 新增 `$KNOWLEDGE` 占位符，注入相关子图 |
| `utils/mcdata.js` | 新增从 minecraft-data 解析配方/属性的逻辑 |

## 收益

- 消除 LLM 对 Minecraft 配方的幻觉
- 减少试错轮次（不需要反复问"这东西怎么合成"）
- 工具需求推理（"挖钻石需要铁镐或以上"）由系统保证
