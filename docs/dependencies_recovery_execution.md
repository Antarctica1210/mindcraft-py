* 增加技能，依赖管理时需要修改的地方

**1. 小型 dependency planner 主体**
改这里：

[recovery.py](/Users/wilson/work_space/mindcraft-py/src/agent/recovery.py:81)

核心函数：

- [RecoveryManager.plan](/Users/wilson/work_space/mindcraft-py/src/agent/recovery.py:84)：决定哪些失败可以恢复。
- [tool_recovery_steps](/Users/wilson/work_space/mindcraft-py/src/agent/recovery.py:173)：缺镐、斧、铲时怎么补工具。
- [missing_material_recovery_steps](/Users/wilson/work_space/mindcraft-py/src/agent/recovery.py:261)：缺 `stick`、`planks`、`crafting_table`、`furnace` 等材料时怎么补。

你列的这几项，大部分应该在这里扩展：

```text
缺镐 -> recovery.py / tool_recovery_steps
缺燃料 -> recovery.py / missing_material_recovery_steps
缺木头 -> recovery.py / ensure_planks / best_inventory_log_item
缺工作台/熔炉 -> recovery.py / missing_material_recovery_steps
```

**2. 执行层批量技能**
改这里：

[executor.py](/Users/wilson/work_space/mindcraft-py/src/agent/executor.py:797)

相关函数：

- [collectAnyWood](/Users/wilson/work_space/mindcraft-py/src/agent/executor.py:744)：接受任意 `*_log`。
- [mineOreWithTool](/Users/wilson/work_space/mindcraft-py/src/agent/executor.py:775)：缺镐时自动做工具再挖矿。
- [craftWithDependencies](/Users/wilson/work_space/mindcraft-py/src/agent/executor.py:797)：自动补木板、木棍、工作台、圆石、基础工具。

如果你要让“直接调用一个命令就自动补依赖”，改 `executor.py`。  
如果你要让“任何失败后都能恢复”，改 `recovery.py`。

**3. 知识图谱 / 规则来源**
改这里：

[knowledge_graph.py](/Users/wilson/work_space/mindcraft-py/src/knowledge/knowledge_graph.py:738)

相关函数：

- [select_tool_for_block](/Users/wilson/work_space/mindcraft-py/src/knowledge/knowledge_graph.py:738)：判断某个方块需要什么工具。
- [get_missing_materials](/Users/wilson/work_space/mindcraft-py/src/knowledge/knowledge_graph.py:893)：判断配方缺什么。
- [get_collection_path](/Users/wilson/work_space/mindcraft-py/src/knowledge/knowledge_graph.py:1012)：获取物品/方块的采集路径。

如果 bot 对“铁矿要石镐”“泥土用铲”“木头用斧”这种基础认知错了，优先查这里。

**4. preflight 层**
改这里：

[command_registry.py](/Users/wilson/work_space/mindcraft-py/src/agent/command_registry.py:168)

和：

[core.py](/Users/wilson/work_space/mindcraft-py/src/agent/core.py:2345)

preflight 负责在命令执行前提前发现问题，比如：

```text
collectBlocks -> 缺工具
craftRecipe -> 缺材料
smeltItem -> 缺熔炉/燃料/输入物
```

如果你希望“缺燃料”不要直接失败，而是进入 recovery，就要保证失败能被 [failures.py](/Users/wilson/work_space/mindcraft-py/src/agent/failures.py:76) 解析成 `missing_materials`。

**5. 测试位置**
主要补这里：

[test_agent_loop_protocols.py](/Users/wilson/work_space/mindcraft-py/tests/test_agent_loop_protocols.py:495)

和：

[test_agent_dynamic_modes.py](/Users/wilson/work_space/mindcraft-py/tests/test_agent_dynamic_modes.py:477)

建议按这几个测试补：

```text
缺石镐 -> 自动做木板/木棍/工作台/石镐 -> 重试 collectBlocks
缺燃料 -> 优先 coal，再 charcoal，再 planks
缺木头 -> 调 collectAnyWood，不固定 oak_log
缺工作台 -> nearby 有就用，没有就做
缺熔炉 -> nearby 有就用，没有就做/放置
```

一句话总结：

```text
依赖规划主逻辑：src/agent/recovery.py
一键批量执行能力：src/agent/executor.py
物品/工具/配方知识：src/knowledge/knowledge_graph.py
执行前拦截：src/agent/core.py + command_registry.py
失败解析：src/agent/failures.py
完成证据：src/agent/effects.py
```

`effects.py` 只记录“做成了什么”，不负责“缺什么该怎么补”。