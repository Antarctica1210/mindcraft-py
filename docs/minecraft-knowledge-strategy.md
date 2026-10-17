# Minecraft 知识层开发策略

本文记录 Phase 7 的知识层设计：如何使用 `minecraft-data` 作为结构化知识底座，以及如何把 Minecraft Wiki 作为补充知识源接入项目。

核心结论：

- 不要把 Minecraft Wiki 整站“变成” `minecraft-data`。
- `minecraft-data` 是结构化、稳定、可执行的游戏数据主来源。
- Minecraft Wiki 更适合作为 supplement / RAG 层，用于补充机制解释、攻略性描述和边缘知识。
- Phase 7 的主路径是结构化知识查询，不是传统文本 embedding RAG。

## Static Knowledge 和 RAG 的关系

Phase 7 的 Static Knowledge 属于 RAG 思路，但更准确地说是：

```text
Structured RAG / Knowledge Tooling
```

传统 RAG：

```text
用户问题 -> embedding 检索 wiki 文档片段 -> 把文本塞给 LLM
```

Phase 7 Static Knowledge：

```text
任务需求 -> 查询结构化 Minecraft 数据/图 -> 返回精确配方、工具链、采集路径 -> LLM 或 ActionExecutor 使用
```

示例：

```json
{
  "target": "enchanting_table",
  "recipe": {
    "book": 1,
    "diamond": 2,
    "obsidian": 4
  },
  "requires": {
    "crafting_station": "crafting_table",
    "tool_requirements": {
      "obsidian": "diamond_pickaxe"
    }
  },
  "missing": ["diamond", "obsidian"],
  "collection_plan": [
    "get diamond_pickaxe",
    "mine obsidian",
    "craft book",
    "craft enchanting_table"
  ]
}
```

这类结果可以直接用于 ActionExecutor 校验，而不是让 LLM 从 wiki 文本里猜。

## 推荐数据分层

```text
minecraft-data
  -> Static Knowledge
  -> 可重建、只读、结构化、可验证

Minecraft Wiki
  -> Supplement / RAG notes
  -> 机制说明、攻略描述、边缘知识、人工资料来源

World Facts
  -> Semantic Memory
  -> bot 在具体世界中观察到的事实
```

三者不要混写。

## minecraft-data 作为主数据源

`minecraft-data` 适合提供：

- blocks
- items
- entities
- recipes
- foods
- effects
- biomes
- materials
- versions
- protocol versions
- windows / container 类型

Phase 7 应优先使用现成 `minecraft-data`，不要先做 Wiki 抓取。

Node 侧示例：

```js
const minecraftData = require('minecraft-data')
const mcData = minecraftData('1.21.4')

console.log(mcData.itemsByName.diamond)
console.log(mcData.blocksByName.obsidian)
console.log(mcData.recipes)
```

Python 侧推荐两种接入方式：

```text
方案 A：Node socket_server.js 提供 queryMinecraftData 请求
方案 B：启动或构建时把 mcData 导出成 JSON，Python 读取 JSON
```

本项目更推荐方案 B，因为 Phase 7 的 KnowledgeGraph 可以完全本地构建，避免运行时跨语言查询过多。

推荐导出目录：

```text
data/minecraft/<version>/
├── items.json
├── blocks.json
├── recipes.json
├── entities.json
├── foods.json
└── graph.json
```

## KnowledgeGraph 建模

不要把 Wiki 文本直接塞给 LLM。应把 `minecraft-data` 转成图或结构化查询索引。

节点示例：

```text
item:diamond
item:stick
block:diamond_ore
recipe:enchanting_table
tool:diamond_pickaxe
entity:cow
```

边示例：

```text
CRAFTS_TO
REQUIRES_ITEM
REQUIRES_TOOL
DROPS
SMELTS_TO
FOUND_IN
CAN_MINE
```

典型查询：

```text
get_recipe(target_item)
get_missing_materials(target_item, inventory)
get_tool_requirements(block)
get_collection_path(target_item)
can_mine(block, equipped_tool)
```

## Minecraft Wiki 作为补充层

Wiki 适合补充：

- 机制说明
- 特殊规则
- 非结构化攻略
- 版本差异说明
- `minecraft-data` 未覆盖的解释性内容

Wiki 不适合直接作为 canonical data：

- 大段自然语言难以直接执行
- 页面结构会变化
- 攻略性描述经常带有“通常来说”
- 许可证和归因要求更复杂
- 与 `minecraft-data` 冲突时需要人工判断

推荐 supplement 格式：

```json
{
  "id": "ancient_debris_mining_tip",
  "type": "mechanic_note",
  "source": "minecraft.wiki",
  "source_url": "https://minecraft.wiki/w/Ancient_Debris",
  "applies_to": ["ancient_debris"],
  "content": "Ancient debris is commonly mined around Y=15 in the Nether.",
  "confidence": "medium",
  "version": "1.21",
  "license": "wiki_source_license",
  "last_checked": "2026-05-10"
}
```

推荐目录：

```text
data/wiki_supplement/
├── mechanics/
├── mining/
├── combat/
└── version_notes/
```

## 如果后续要从 Wiki 抽取

不要直接抓整站后塞进向量库。推荐管线：

```text
Minecraft Wiki API / 页面 HTML
  -> 页面类型识别：item / block / entity / recipe / mechanic
  -> 表格和 infobox 解析
  -> 名称归一化到 minecraft-data id
  -> schema validate
  -> 和 minecraft-data 对比
  -> 写入 supplement JSON
  -> 人工审核或测试
```

只有结构化、可验证的数据才考虑进入主图。

适合进入主图：

- 配方
- 物品 ID
- 方块 ID
- 工具等级
- 掉落物
- 食物值
- 实体属性
- 熔炼关系

不适合直接进入主图：

- Wiki 大段说明
- 玩家攻略
- “通常来说”
- 版本争议
- 未经验证的行为建议

## 与 MemoryLayer 的关系

Static Knowledge 默认只读，不频繁写 MemoryLayer。

```text
Static Knowledge:
  来源：minecraft-data
  存储：data/minecraft/<version>/
  生命周期：随版本重建
  用途：配方、工具链、采集路径校验
```

World Facts 才写入 MemoryLayer：

```text
World Facts:
  来源：bot 观察 / 玩家标记 / 执行结果
  存储：bots/<name>/memory/facts/
  生命周期：随世界变化，需要 ttl/confidence/evidence
  用途：基地坐标、箱子库存、矿点、传送门、危险区域
```

示例：

```text
obsidian requires diamond_pickaxe
  -> Static Knowledge

base chest contains 12 obsidian at (-120, 64, 40)
  -> World Fact / Semantic Memory
```

冲突处理：

```text
静态知识和世界事实冲突时，静态知识优先。
世界事实过期或验证失败时，降低 confidence 或 forget。
Wiki supplement 和 minecraft-data 冲突时，不覆盖主数据，进入 review。
```

## Phase 7 推荐实现

```text
src/knowledge/knowledge_graph.py
  读取 data/minecraft/<version>/*.json
  构建配方 / 采集 / 工具链图

src/knowledge/world_facts.py
  将运行时 LOCATED_AT / STORED_IN / DANGEROUS_NEAR 等事实写入 MemoryLayer

scripts/export_minecraft_data.js
  从 npm minecraft-data 导出指定版本 JSON

data/minecraft/<version>/
  minecraft-data 导出的结构化数据

data/wiki_supplement/
  后期可选的 Wiki 补充知识
```

第一版优先级：

1. 导出 items / blocks / recipes / entities。
2. 实现 `get_recipe()` / `get_tool_requirements()`。
3. 实现 `get_missing_materials()`。
4. 实现简单 BFS 合成路径。
5. ActionExecutor 执行合成/采集前调用 KnowledgeGraph 校验。
6. Wiki supplement 暂缓，等主知识图稳定后再做。

## 最终原则

`minecraft-data` 是 Phase 7 的 canonical structured data。  
Minecraft Wiki 是补充说明和 RAG 来源，不是主数据源。  
ActionExecutor 应依赖结构化查询结果，而不是依赖 LLM 读 Wiki 后自由发挥。
