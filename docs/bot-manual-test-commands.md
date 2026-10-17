# Bot Manual Test Commands

这份清单用于手动回归测试 bot。建议每次只发一条命令，等 bot 明确完成、失败或卡住后再发下一条，然后根据 `logs/*.log`、`logs/*.jsonl` 和 `log-js/*` 做 debug。

## 测试前准备

- 使用一个相对安全的生存世界，白天开始更稳定。
- 玩家站在 bot 附近，背包最好先清空或记录初始物品。
- 如果要测交付，玩家需要保持在 bot 可追踪范围内。
- 如果要测合成/烹饪，附近可以刻意放一个工作台和炉子，用来验证 bot 会不会复用已有工具。
- 每条测试后记录：玩家原话、bot 最后一句话、任务状态、背包变化、是否交付成功。

## 基础观察与控制

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| B01 | `inventory` | 查询命令、背包读取 | bot 回复当前背包 | `method=get_inventory` |
| B02 | `stats` | 基础 observe | bot 回复血量、饥饿、位置等 | `method=observe` |
| B03 | `what blocks are nearby?` | `nearbyBlocks`、16 格扫描 | bot 回复附近方块 | `method=get_nearby_blocks` |
| B04 | `come to me` | 直接动作、玩家定位、导航 | bot 靠近玩家 | `go_to_player`、`action_result` |
| B05 | `follow me` | 跟随目标、长期 goal | bot 开始跟随 | `follow_player`、`Current goal` |
| B06 | `stop` | 停止导航/动作 | bot 停止移动或跟随 | `method=stop` |

## 搜索与移动

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| M01 | `find an oak log` | `searchForBlock` 默认 64 | bot 找到并走向橡木 | `search_for_block`、range 不超过 64 |
| M02 | `find a crafting table` | 工具方块搜索 | 找到已有工作台，或失败说明没有 | `search_for_block crafting_table` |
| M03 | `find a furnace` | 炉子搜索 | 找到已有炉子，避免后续重复造炉 | `search_for_block furnace` |
| M04 | `find a sheep` | `searchForEntity` 默认 64 | bot 找到并走向羊 | `search_for_entity` |
| M05 | `move away 10 blocks` | 随机移动、pathfinder | bot 离开当前位置 | `move_away` |

## 采集与实体掉落

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| G01 | `collect 3 oak logs` | 方块采集、工具检查 | 背包增加 `oak_log` | `collect_blocks`、Inventory delta |
| G02 | `collect 8 cobblestone` | 挖矿、工具需求 | 有镐时成功；无镐时提示需要工具 | `Need a ... to mine` |
| G03 | `get some wool` | 实体资源推理、攻击羊 | 背包增加 wool，可能需要多只羊 | `searchForEntity -> attack` |
| G04 | `get leather from a cow` | 被动生物掉落 | 背包增加 leather 或 beef | `attack cow` |
| G05 | `kill a zombie` | 敌对实体战斗 | 找到并攻击僵尸；没有则失败 | `attack zombie` |

## 合成

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| C01 | `craft oak planks` | 简单 2x2 合成 | 背包增加 `oak_planks` | `craft_recipe oak_planks` |
| C02 | `craft sticks` | 依赖已有木板 | 背包增加 `stick` | `craft_recipe stick` |
| C03 | `craft a crafting table` | 基础工作台合成 | 背包增加 `crafting_table` | `craft_recipe crafting_table` |
| C04 | `craft a wooden pickaxe` | 多步骤合成链 | 先木板/木棍，再 pickaxe | `craftRecipe` 多轮、missing materials |
| C05 | `craft a furnace` | 石头需求、已有炉子跳过 | 若附近已有炉子，应跳过重复造；否则用 8 cobblestone 合成 | `SKIPPED: furnace is already nearby` |
| C06 | `craft a white bed` | 羊毛、木板、工作台、交付 | 成功合成并交给玩家 | `white_bed`、`givePlayer`、`Gave 1x` |
| C07 | `craft a bed` | 物品名纠错 | 应推断具体颜色床，避免一直尝试 `bed` | `resolve_craft_target`、`white_bed` |

## 烹饪与炉子复用

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| S01 | `cook some pork` | 直接烹饪路径、炉子复用、交付 | 使用已有炉子或必要时造炉，烧好后交付 | `searchForBlock furnace`、`smeltItem`、`givePlayer` |
| S02 | `cook 2 mutton` | 数量解析、燃料选择 | 得到并交付 2 个 cooked_mutton | `Smelted 2x`、`Gave 2x` |
| S03 | `smelt one raw iron` | 矿物烧炼 | 得到 `iron_ingot`，若缺燃料则明确失败 | `bestSmeltingFuel`、`putFuel` |
| S04 | `clear the furnace` | 炉子窗口操作 | 清空附近炉子输入/燃料/输出 | `clear_furnace` |

## 交付与玩家追踪

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| D01 | `give me 1 dirt` | `givePlayer` 成功标准 | 实际丢出至少 1 个物品才算成功 | `Gave 1x dirt` |
| D02 | `give me a stick` | 自动补全玩家名、交付 | bot 走到玩家附近并丢出 stick | `go_to_player`、`tossed` |
| D03 | `give me 99 diamonds` | 缺物品失败 | 不应标记成功 | `Could not deliver`、`No requested item` |
| D04 | `craft a torch and give it to me` | 合成后交付链 | 成品必须交付，不能只说完成 | `task_finish status=succeeded` 前应有 `givePlayer` |

## 工作台/炉子复用专项

这些命令建议在玩家附近先放好 `crafting_table` 和 `furnace` 后测试。

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| R01 | `craft a wooden shovel` | 附近工作台复用 | 使用已有工作台，不应重复放置 | `crafting_table is already nearby` 或无 `placeHere crafting_table` |
| R02 | `craft a furnace` | 附近炉子复用 | 如果附近已有炉子，应跳过 craft furnace | `SKIPPED: furnace is already nearby` |
| R03 | `cook some pork` | 附近炉子复用 | 先搜索到炉子再烧，不应先造新炉子 | `search_for_block furnace` 成功后直接 `smelt_item` |

## 失败恢复与防重复

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| F01 | `collect 10 diamond ore` | 不可达/缺工具/找不到 | 失败原因清晰，不应无限重试 | `FAILED`、`3 consecutive failures` |
| F02 | `craft a saddle` | 无配方处理 | 明确无配方或无法合成 | `No recipe` |
| F03 | `cook 10 porkchops` | 数量不足 | 应说明原料不足，不应假成功 | `Not enough porkchop` |
| F04 | `give me cooked porkchop` | 无成品交付 | 没有成品时应失败或先烹饪，不应 `Gave 0x` | `tossed 0` 不应成功 |
| F05 | `find ancient_debris` | 搜索上限 | 搜索范围不超过 64，失败可接受 | `search_range`、`64 block radius` |

## 动态代码与复杂动作

这些测试风险更高，建议确认 `newAction` 已按你的安全策略启用后再跑。

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| N01 | `build a dirt tower 5 blocks tall here` | `newAction`、放置循环 | 建一根 5 高 dirt tower | `execute_code`、JS trace |
| N02 | `build a small 3 by 3 cobblestone platform here` | 坐标/放置 | 建简单平台 | `newAction`、block placement |
| N03 | `put 3 porkchops and fuel into the nearby furnace` | 容器窗口动态代码 | 不应导致 socket 断开；失败要可恢复 | `windowOpen`、`Socket server closed connection` |

## Wiki RAG 游戏内冒烟

这些用例需要真实 LLM + Minecraft 服务器，不进入 mock bridge 自动回归。建议先用小索引验证链路，再扩到完整 wiki 索引。

测试前环境：

```bash
WIKI_RAG_ENABLED=true
WIKI_RAG_INDEX_DIR=data/wiki-test/chicken
WIKI_RAG_EMBED_MODEL=models/BAAI/bge-m3
WIKI_RAG_RERANK_MODEL=models/BAAI/bge-reranker-v2-m3
WIKI_RAG_DEVICE=cpu
WIKI_RAG_USE_FP16=false
```

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| WIKI_RAG_CHICKEN_01 | `Use the wiki knowledge about chickens, then breed two nearby chickens with the right seed.` | 本地 Wiki RAG 加载、`searchWiki`、Chicken 页面检索、`breedAnimals` 繁殖流程 | bot 应从 Chicken 知识中选择 `wheat_seeds` 等种子，调用 `breedAnimals("chicken", "wheat_seeds")`，找到/接近两只鸡并喂食；若缺鸡或缺种子，应清晰失败 | `Wiki RAG loaded from data/wiki-test/chicken`、`searchWiki`、`Chicken`、`wheat_seeds`、`breedAnimals` |

手动布置：

- 玩家和 bot 附近放两只成年 chicken。
- bot 背包里放至少 2 个 `wheat_seeds`。
- 白天、安全平地测试，避免敌对生物打断。
- 通过测试的标准：两只鸡进入 love mode 并生成 baby chicken；或者在缺鸡/缺种子时明确说明资源不足，不要胡乱攻击鸡或声称成功。

## 长流程回归

| ID | 玩家输入 | 主要覆盖 | 预期结果 | 重点看日志 |
| --- | --- | --- | --- | --- |
| L01 | `craft a bed and give it to me` | 搜索实体、攻击、合成、交付 | 最终玩家收到床 | 全链路事件、`task_finish succeeded` |
| L02 | `get wood, craft a wooden pickaxe, mine stone, craft a furnace, cook pork, and give it to me` | 多阶段任务 | 完成或在明确资源不足处失败 | 重复命令、状态刷新、交付 |
| L03 | `make 8 blocks of wheat farmland` | till/sow、工具和种子 | 成功耕地播种或说明缺少 hoe/seeds | `tillAndSow`、inventory |
| L04 | `come to me, then follow me for a while, then stop when I say stop` | 玩家交互和长期 goal | follow 可启动，stop 可停止 | `GoalFollow`、`stop` |

## Debug 记录模板

每次测试建议复制下面模板到你的调试记录里：

```text
Test ID:
Player command:
Initial inventory:
Nearby tools/blocks:
Expected:
Actual:
Final inventory:
Was item delivered? yes/no
Key log lines:
Suspected layer:
  [ ] intent/router
  [ ] LLM plan
  [ ] command parsing
  [ ] Python executor/core
  [ ] socket bridge
  [ ] socket server
  [ ] Mineflayer/pathfinder/window
Notes:
```

## 日志关键词速查

- 任务开始：`Plan start`
- LLM 输出：`Plan response`、`<<<`
- 命令执行：`Bot action step start`
- socket 请求：`SocketBridge request start`
- socket 成功：`SocketBridge request done`
- 命令失败：`ActionExecutor failed`
- 动态代码：`execute_code`
- 容器窗口问题：`windowOpen`、`openFurnace`、`openContainer`
- socket 崩溃：`Socket server closed connection`
- 任务完成：`ActiveTask finish`
- 自动交付：`Auto-delivering active task result`
- 交付成功：`Gave Nx`
