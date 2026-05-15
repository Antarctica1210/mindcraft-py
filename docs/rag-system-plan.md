# Minecraft RAG 系统实施方案

本文给出一个可落地的 RAG 系统方案，用来补充现有 `KnowledgeGraph`。目标不是让 LLM 直接读一堆百科文本后自由发挥，而是建立一套“结构化知识优先、Wiki 片段补充、执行前校验”的规划链路。

## 目标

- 让 bot 能回答和执行更开放的 Minecraft 任务，例如养鸡、种田、繁殖动物、建基础农场、寻找资源。
- 减少 LLM 胡编游戏规则，例如“鸡会掉种子”“熟肉可以合成”“没有工具也能挖矿石”。
- 让百科知识以可追踪来源的形式进入 prompt，而不是污染长期记忆或硬编码进代码。
- 让成百上千个流程以数据和检索方式维护，而不是写成成百上千个 Python 分支。

## 总体架构

```text
User request
  -> InputRouter / DispatchGate
  -> Observe current world state
  -> Normalize target names with KnowledgeGraph
  -> Structured knowledge query
  -> Wiki RAG retrieval if needed
  -> Build bounded planning context
  -> LLM creates next action or short plan
  -> Action validator checks against KnowledgeGraph
  -> ActionExecutor executes
  -> Feedback updates world facts / task memory
```

核心原则：

- `minecraft-data` 是硬规则来源。
- Minecraft Wiki 是补充说明来源。
- MemoryLayer 存世界事实，不存通用游戏规则。
- LLM 只能在检索到的事实和当前 observe 约束内规划。

## 数据分层

### Canonical Structured Data

目录：`data/minecraft/1.21.6/`

当前已有：

- `items.json`
- `blocks.json`
- `entities.json`
- `foods.json`
- `recipes.json`
- `blockLoot.json`
- `entityLoot.json`
- `materials.json`
- `biomes.json`
- `features.json`
- `version.json`

用途：

- 名称归一化。
- 配方和缺料计算。
- 方块/实体掉落判断。
- 工具需求判断。
- 食物价值判断。
- 动作前校验。

### Wiki RAG Data

推荐目录：

```text
data/wiki_rag/
├── raw/
├── parsed/
├── chunks/
├── index/
└── manifests/
```

用途：

- 补充机制流程，例如“怎么繁殖鸡”“作物如何生长”“村民交易限制”。
- 补充 `minecraft-data` 不直接表达的条件和技巧。
- 给 LLM 提供流程 example，而不是提供无约束百科原文。

### World Facts

目录：`bots/<bot_name>/memory/`

用途：

- 玩家基地位置。
- 附近箱子库存。
- 发现过的鸡/牛/村庄/矿点。
- 已建好的农场和工作站。

这些事实会随世界变化，需要 confidence、evidence、ttl 或重新验证。

## Wiki 数据获取

第一版不要抓整站。先做白名单页面：

```text
Chicken
Cow
Sheep
Pig
Animal
Breeding
Wheat Seeds
Wheat
Grass
Tall Grass
Furnace
Smelting
Food
Crop Farming
Villager
Trading
```

获取方式：

- MediaWiki API：`https://minecraft.wiki/api.php`
- 单页导出：`https://minecraft.wiki/w/Special:Export/<PageName>`

建议优先用 MediaWiki API 拉页面 HTML 或 wikitext，然后本地解析。每次抓取写 manifest：

```json
{
  "page": "Chicken",
  "source_url": "https://minecraft.wiki/w/Chicken",
  "retrieved_at": "2026-05-14",
  "minecraft_version": "1.21.6",
  "parser_version": 1,
  "content_hash": "..."
}
```

## Chunk Schema

不要只存纯文本。每个 chunk 必须带结构化 metadata：

```json
{
  "id": "minecraft_wiki:chicken:breeding:001",
  "source": "minecraft.wiki",
  "page": "Chicken",
  "section": "Breeding",
  "source_url": "https://minecraft.wiki/w/Chicken#Breeding",
  "kind": "mechanic",
  "applies_to": ["chicken", "wheat_seeds", "breeding"],
  "entities": ["chicken"],
  "items": ["wheat_seeds", "egg"],
  "blocks": [],
  "actions": ["breed", "feed", "collect"],
  "version": "1.21.6",
  "confidence": "medium",
  "text": "Short, cleaned, section-level text...",
  "facts": [
    {
      "subject": "chicken",
      "predicate": "breeding_item",
      "object": "wheat_seeds"
    }
  ]
}
```

字段说明：

- `kind`: `mechanic`, `strategy`, `warning`, `version_note`, `how_to`, `drop_note`。
- `applies_to`: 检索主键，必须尽量归一化到 `minecraft-data` 名称。
- `facts`: 可选，只有高置信结构化抽取才写入。
- `text`: 控制在 300 到 900 tokens，不要塞整页。

## 解析策略

页面处理流程：

```text
Raw page
  -> remove nav/sidebar/templates unrelated to content
  -> split by h2/h3 section
  -> classify section kind
  -> extract links/items/entities/blocks
  -> normalize names through KnowledgeGraph
  -> validate extracted facts
  -> write chunks
```

第一版只保留这些 section 类型：

- Obtaining
- Usage
- Breeding
- Farming
- Smelting
- Drops
- Behavior
- Trading
- Mechanics

跳过：

- History
- Sounds
- Data values
- Gallery
- Trivia
- Issues

## 索引设计

第一版可以同时建两个索引：

### Symbol Index

本质是倒排表：

```json
{
  "chicken": ["minecraft_wiki:chicken:breeding:001"],
  "wheat_seeds": ["minecraft_wiki:chicken:breeding:001", "minecraft_wiki:wheat_seeds:obtaining:001"],
  "breeding": ["minecraft_wiki:animal:breeding:001"]
}
```

优点：

- 快。
- 稳。
- 很适合 Minecraft 这种实体/物品/方块名明确的领域。

### Vector Index

用于模糊查询和流程类问题。

第一版可以用本地 embedding 模型或 LM Studio 暴露的 embedding API；如果没有配置 embedding，就退回 symbol index。

索引字段：

```text
embedding_text = page + section + kind + applies_to + text
```

检索时保留原始 metadata，方便过滤和归因。

## 检索流程

输入：

```text
玩家请求：养鸡
observe：附近有 chicken x2，背包有 wheat_seeds x8，附近有 fence x2
```

流程：

1. 用 `KnowledgeGraph` 归一化关键词：`chicken`, `wheat_seeds`, `breeding`。
2. Symbol index 取相关 chunk。
3. Vector index 取语义相近 chunk。
4. 用当前 observe 做过滤：
   - 有鸡：优先 breeding/chicken chunk。
   - 没种子：加入 wheat_seeds obtaining chunk。
   - 没鸡：加入 finding/egg/spawn 相关 chunk。
5. 用 `KnowledgeGraph` 校验硬规则：
   - chicken 不掉 wheat_seeds。
   - tall_grass 可掉 wheat_seeds。
   - chicken breeding item 是 seeds 类。
6. 给 LLM 注入最多 3 到 6 个高相关 chunk。

## Prompt 注入格式

不要把 chunk 原样全部塞进去。推荐压成：

```text
Knowledge context:
- Source: minecraft-data
  Fact: chicken does not drop wheat_seeds.
  Fact: tall_grass may drop wheat_seeds.
  Fact: chicken entity exists and is passive.

- Source: Minecraft Wiki / Chicken / Breeding
  Chickens can be bred by feeding seeds to two adult chickens. Use wheat_seeds when available.

- Source: Minecraft Wiki / Wheat Seeds / Obtaining
  Wheat seeds can be obtained by breaking grass-like plants.

Current world:
- Inventory: wheat_seeds x8, egg x8
- Nearby entities: chicken x2
- Nearby blocks: tall_grass, fence

Planning constraints:
- Prefer existing nearby resources.
- Do not attack chickens when the goal is breeding or seeds.
- Use one command at a time.
```

## Action Validation

LLM 输出命令后，执行前跑 validator：

```text
validate_action(command, args, goal, knowledge_context, bot_state)
```

第一版规则：

- 如果目标是 `wheat_seeds`，拦截 `attack("chicken")`。
- 如果目标是 breeding，拦截攻击同类动物。
- 如果目标是 cooked food，禁止 `craftRecipe(cooked_*)`，改用 `smeltItem`。
- 如果目标是 fragile block drops，优先 `breakNearbyBlocks` 而不是 `collectBlocks`。
- 如果采集矿石缺工具，先要求 craft/search 工具。
- 如果 Wiki chunk 与 `minecraft-data` 冲突，`minecraft-data` 优先。

## 与 KnowledgeGraph 的衔接

新增接口建议：

```python
class KnowledgeGraph:
    def normalize_name(self, text: str, kind: str | None = None) -> str | None: ...
    def get_block_drops(self, block_name: str) -> list[dict]: ...
    def get_entity_drops(self, entity_name: str) -> list[dict]: ...
    def drops_item(self, source_name: str, item_name: str, *, source: str) -> bool: ...
    def get_obtain_plan(self, item_name: str, quantity: int, state: BotState) -> dict: ...
```

RAG 层不应该重复实现这些硬规则。RAG 只做补充检索，最终事实要经过 KG 归一化和校验。

## 模块划分

推荐文件结构：

```text
src/rag/
├── __init__.py
├── wiki_fetcher.py
├── wiki_parser.py
├── chunk_schema.py
├── chunk_store.py
├── symbol_index.py
├── vector_index.py
├── retriever.py
├── prompt_context.py
└── validator.py

scripts/
├── fetch_wiki_pages.py
├── build_wiki_chunks.py
└── build_rag_index.py
```

第一版可以不引入数据库：

```text
data/wiki_rag/chunks/*.jsonl
data/wiki_rag/index/symbol_index.json
data/wiki_rag/index/vector_index.npz 或 faiss.index
```

## 实施阶段

### Phase 1: Offline Wiki Ingestion

目标：

- 抓取白名单页面。
- 按 section 切 chunk。
- 写入 metadata。
- 建 symbol index。

验收：

- `Chicken` 页面能产出 breeding/egg/drops/behavior chunks。
- `Wheat Seeds` 页面能产出 obtaining/usage chunks。
- chunk 中的 item/entity 名称能归一化到 `minecraft-data`。

### Phase 2: Retriever

目标：

- 实现 `retrieve_knowledge(query, bot_state, limit=6)`。
- 支持 symbol 检索。
- 如果 embedding 配置存在，增加 vector 检索。
- 对结果做去重、排序、来源归因。

排序信号：

- `applies_to` 与目标实体/物品匹配。
- section kind 与任务意图匹配。
- 当前 observe 中存在相关实体/物品/方块。
- `minecraft-data` 校验通过。

### Phase 3: Planner Integration

目标：

- 在 plan prompt 中加入 `Knowledge context`。
- 对复杂任务才启用 RAG。
- 简单合成/采集仍优先 KG 和直接动作。

触发条件：

- 请求包含 breeding/farming/building/trading/mechanic。
- KG 无法给出完整 obtain plan。
- LLM 第一次失败后需要补充机制知识。

### Phase 4: Action Validator

目标：

- 在 `_execute_agent_command` 前增加规则校验。
- 拦截明显错误动作。
- 给 LLM 返回短 feedback，让它下一轮修正。

示例反馈：

```text
Rejected action: attack("chicken")
Reason: current goal is obtaining wheat_seeds; KnowledgeGraph says chicken drops feather and chicken, not wheat_seeds.
Recommended: breakNearbyBlocks("tall_grass", 8)
```

### Phase 5: Evaluation

增加手动和自动用例：

- `get 8 wheat seeds`
- `breed chickens`
- `make a small chicken pen`
- `farm wheat`
- `cook mutton`
- `craft a bed and give it to me`

指标：

- 错误规则动作减少。
- 平均 LLM 轮数下降。
- 任务失败原因更具体。
- prompt token 不显著膨胀。

## 风险和约束

- Wiki 内容有许可证和归因要求，保留 source_url 和 manifest。
- Wiki 页面结构会变，parser 要容错。
- 版本差异可能导致 Wiki 与 `minecraft-data` 冲突，冲突时 KG 优先。
- RAG 片段不能无限注入，默认最多 6 条。
- 不要把检索到的通用知识写入 MemoryLayer。

## 最小可行版本

最小 MVP 只需要：

1. 白名单抓取 `Chicken`, `Wheat Seeds`, `Tall Grass`, `Breeding`。
2. section 级 chunk。
3. symbol index。
4. prompt 注入 3 条以内。
5. validator 拦截 `attack chicken -> wheat_seeds`。

这个 MVP 就能解决当前日志里最明显的问题：bot 不再为了拿种子去攻击鸡，也能在养鸡任务里知道要用种子喂两只鸡。

## 推荐下一步

先实现 offline ingestion 和 symbol index。不要一开始就上复杂向量库。Minecraft 的实体、物品、方块名高度结构化，symbol index 加 KG 校验已经能覆盖大量场景。等白名单页面和 validator 稳定后，再加 embedding 检索作为模糊补充。
