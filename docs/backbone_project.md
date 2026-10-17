主干有两层，容易混在一起：

**1. 程序入口 / 事件接线**
在 [main.py](/Users/wilson/work_space/mindcraft-py/main.py:169)。

这里创建 `Agent`，然后把 Minecraft chat 事件接到：

```python
await agent.handle_message(sender, message)
```

同时启动后台 tick loop：

```python
self._loop_task = asyncio.create_task(agent.run_loop())
```

**2. 真正的对话任务 Agent Loop**
核心在 [src/agent/core.py](/Users/wilson/work_space/mindcraft-py/src/agent/core.py:1069) 的 `Agent.handle_message()`。

它负责：

```text
玩家消息
-> InputRouter 分类
-> 创建/替换 ActiveTask
-> _run_plan_loop()
```

然后 `_run_plan_loop()` 在 [src/agent/core.py](/Users/wilson/work_space/mindcraft-py/src/agent/core.py:1271)，只是转发给：

[src/agent/planner_loop.py](/Users/wilson/work_space/mindcraft-py/src/agent/planner_loop.py:105) 的 `PlannerLoop.run()`。

这里才是最核心的 observe-plan-act-check 循环：

```text
observe()
-> direct shortcut / skill / completion check
-> LLM plan()
-> parse_response()
-> 执行 command
-> 记录 result/effects/failure
-> recovery / completion 判断
-> 下一轮
```

**3. 后台 tick loop**
在 [src/agent/core.py](/Users/wilson/work_space/mindcraft-py/src/agent/core.py:3654) 的 `Agent.run_loop()`。

这个不是主任务规划 loop，它主要做：

```text
SafetyMonitor interrupt
quick observe
modes.tick()
unstuck 检查
```

所以如果你要改“bot 接到任务后怎么规划、行动、失败恢复、完成判断”，主要看：

- [src/agent/core.py](/Users/wilson/work_space/mindcraft-py/src/agent/core.py:1069) `handle_message`
- [src/agent/planner_loop.py](/Users/wilson/work_space/mindcraft-py/src/agent/planner_loop.py:111) `PlannerLoop.run`
- [src/agent/core.py](/Users/wilson/work_space/mindcraft-py/src/agent/core.py:2323) `_recover_missing_dependency`
- [src/agent/core.py](/Users/wilson/work_space/mindcraft-py/src/agent/core.py:1288) `_maybe_finish_after_command_completion`

一句话：**主干任务 loop 在 `planner_loop.py::PlannerLoop.run()`，入口和任务状态管理在 `core.py::handle_message()`。**