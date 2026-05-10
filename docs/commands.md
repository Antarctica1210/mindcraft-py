# 机器人命令参考

LLM 智能体可用的全部 52 个移植命令。命令使用 `!commandName(args)` 语法。

## 控制类

| 命令 | 参数 | 描述 |
|---|---|---|
| `!stop()` | — | 停止所有当前动作（移动、挖掘、战斗、跟随）。 |
| `!stfu()` | — | 停止聊天和自我叙述，但继续当前动作。 |
| `!restart()` | — | 重启智能体进程（干净关闭）。 |
| `!clearChat()` | — | 清除对话历史。 |

## 观察 / 查询类

| 命令 | 参数 | 描述 |
|---|---|---|
| `!stats()` | — | 报告当前生命值、饥饿度、位置、游戏模式、生物群系、时间、天气和附近玩家。 |
| `!inventory()` | — | 列出机器人背包中所有物品及其数量。 |
| `!goal()` | — | 报告当前寻路目标（如有）。 |
| `!entities()` | — | 列出附近玩家和实体及其位置和类型。 |
| `!nearbyBlocks()` | — | 列出机器人附近的方块类型。 |
| `!craftable()` | — | 列出当前背包可合成的所有物品。 |
| `!help()` | — | 列出所有可用命令。 |
| `!modes()` | — | 显示所有可用行为模式及其状态。 |

## 移动类

| 命令 | 参数 | 描述 |
|---|---|---|
| `!moveTo(x, y, z)` | `x: float`，`y: float`，`z: float` | 将机器人移动到指定坐标。 |
| `!goToPlayer(player_name, closeness?)` | `player_name: string`，`closeness: float`（默认 3） | 导航到指定玩家。 |
| `!followPlayer(player_name, follow_dist?)` | `player_name: string`，`follow_dist: float`（默认 4） | 持续跟随一个玩家。 |
| `!goToCoordinates(x, y, z, closeness?)` | `x: float`，`y: float`，`z: float`，`closeness: float`（默认 2） | 移动到指定坐标。 |
| `!searchForBlock(block_type, search_range?)` | `block_type: string`（如 `oak_log`），`search_range: float`（默认 64） | 寻找并导航到最近的指定方块。 |
| `!searchForEntity(entity_type, search_range?)` | `entity_type: string`（如 `cow`），`search_range: float`（默认 64） | 寻找并导航到最近的指定实体。 |
| `!moveAway(distance?)` | `distance: float`（默认 16） | 向随机方向移动指定距离。 |
| `!goToBed()` | — | 找到最近的床并睡觉。 |
| `!goToSurface()` | — | 从地下向上导航到地表。 |
| `!digDown(distance?)` | `distance: int`（默认 10） | 向下挖掘，遇到岩浆/水/基岩时停止。 |
| `!stay(seconds?)` | `seconds: int`（-1 = 无限） | 原地待命。暂停所有模式。 |

## 战斗类

| 命令 | 参数 | 描述 |
|---|---|---|
| `!attack(entity_type)` | `entity_type: string`（如 `zombie`） | 攻击并击杀最近的指定类型敌对实体。 |
| `!attackPlayer(player_name)` | `player_name: string` | 攻击指定玩家直到其死亡或逃跑。 |

## 背包管理

| 命令 | 参数 | 描述 |
|---|---|---|
| `!equip(item_name)` | `item_name: string`（如 `diamond_sword`） | 将物品装备到指定身体槽位。 |
| `!consume(item_name)` | `item_name: string`（如 `cooked_beef`） | 食用或饮用指定物品。 |
| `!discard(item_name, num?)` | `item_name: string`，`num: int`（-1 = 全部） | 从背包丢弃物品。 |
| `!givePlayer(player_name, item_name, num?)` | `player_name: string`，`item_name: string`，`num: int`（默认 1） | 导航到玩家并给予物品。 |

## 方块交互

| 命令 | 参数 | 描述 |
|---|---|---|
| `!placeHere(block_type)` | `block_type: string` | 在当前位置放置背包中的方块。 |
| `!activateBlock(block_type)` | `block_type: string`（如 `lever`） | 右键点击/激活最近的指定类型方块。 |
| `!collectBlocks(blockType, count?)` | `blockType: string`（如 `oak_log`），`count: int` | 挖掘并收集指定类型的方块。 |
| `!useOn(tool_name, target)` | `tool_name: string`（如 `bucket`），`target: string`（实体/方块名，或 `nothing`/`hand`） | 对目标实体或方块使用工具。 |

## 箱子操作

| 命令 | 参数 | 描述 |
|---|---|---|
| `!viewChest()` | — | 显示最近箱子的内容物。 |
| `!putInChest(item_name, num?)` | `item_name: string`，`num: int`（-1 = 全部） | 将物品存入最近的箱子。 |
| `!takeFromChest(item_name, num?)` | `item_name: string`，`num: int`（-1 = 全部） | 从最近的箱子取出物品。 |

## 合成 / 烧炼

| 命令 | 参数 | 描述 |
|---|---|---|
| `!craftRecipe(recipe_name, num?)` | `recipe_name: string`（如 `stick`），`num: int`（默认 1） | 使用可用材料合成配方。 |
| `!smeltItem(item_name, num?)` | `item_name: string`（如 `raw_iron`），`num: int`（默认 1） | 在熔炉中烧炼物品。 |
| `!clearFurnace()` | — | 清空最近熔炉中的所有物品。 |

## 村民交易

| 命令 | 参数 | 描述 |
|---|---|---|
| `!showVillagerTrades(villager_id)` | `villager_id: int` | 按实体 ID 显示村民的交易列表。 |
| `!tradeWithVillager(villager_id, trade_index?, count?)` | `villager_id: int`，`trade_index: int`（从 1 开始，默认 1），`count: int`（默认 1） | 与村民执行交易。 |

## 视角

| 命令 | 参数 | 描述 |
|---|---|---|
| `!lookAtPlayer(player_name)` | `player_name: string` | 看向某个玩家或与该玩家同方向。 |
| `!lookAtPosition(x, y, z)` | `x: float`，`y: float`，`z: float` | 看向指定坐标。 |

## 模式 / 目标

| 命令 | 参数 | 描述 |
|---|---|---|
| `!setMode(mode_name, on?)` | `mode_name: string`，`on: bool`（默认 true） | 启用或禁用某个行为模式。 |
| `!setGoal(self_prompt)` | `self_prompt: string` | 设置用于持久自我提示的目标信息。 |
| `!endGoal()` | — | 结束当前目标并停止自我提示。 |

## 技能类

| 命令 | 参数 | 描述 |
|---|---|---|
| `!pickupNearbyItems(distance?)` | `distance: float`（默认 8） | 拾取地面上所有附近的掉落物。 |
| `!tillAndSow(x, y, z, seed_type?)` | `x: float`，`y: float`，`z: float`，`seed_type: string`（可选，如 `wheat_seeds`） | 耕地并可选种植种子。 |
| `!useDoor(x?, y?, z?)` | `x: float`（可选），`y: float`（可选），`z: float`（可选） | 开门、通过并关门。未传坐标时自动找最近的门。 |
| `!getCraftingPlan(target_item, quantity?)` | `target_item: string`（如 `diamond_sword`），`quantity: int`（默认 1） | 获取详细的合成计划，显示所需材料和步骤。 |
| `!searchWiki(query)` | `query: string`（如 `enchanting table`） | 在 Minecraft Wiki 中搜索关于物品、方块或机制的信息。 |

## 对话类

| 命令 | 参数 | 描述 |
|---|---|---|
| `!startConversation(player_name, message)` | `player_name: string`，`message: string` | 与另一玩家开始对话。 |
| `!endConversation(player_name)` | `player_name: string` | 结束当前对话。 |

---

## 行为模式

`!setMode` 可用的模式：

| 模式 | 描述 |
|---|---|
| `self_preservation` | 自动逃离火焰、岩浆等危险。 |
| `unstuck` | 当机器人停止前进时检测并尝试脱困。 |
| `cowardice` | 逃离敌对生物。 |
| `self_defense` | 自动攻击进入范围内的敌对生物。 |
| `hunting` | 狩猎动物获取食物和资源。 |
| `item_collecting` | 自动拾取地面掉落物。 |
| `torch_placing` | 在黑暗区域放置火把以防止怪物生成。 |
| `elbow_room` | 当玩家过于靠近时移开。 |

## 架构

```
LLM (Claude/GPT) → !command(args) → ActionExecutor → SocketBridge (TCP) → socket_server.js → Mineflayer → Minecraft
```
