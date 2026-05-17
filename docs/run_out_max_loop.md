没有完成的直接原因是：**不是动作失败，而是规划循环用完了次数预算**。

日志里这句是关键：

`status=failed attempts=19 failures=0`

说明 19 次动作里没有执行错误，但任务目标是“种 43 个种子”，实际还没达到完成条件，planner loop 到达本轮最大步数后触发了：

`Plan loop ended before completion spec was satisfied.`

所以它被判定失败。

还有两个重要问题：

1. **任务太大，执行粒度太细**
   Bot 基本是 `searchForBlock` 找一块草地，再 `tillAndSow` 种一格。43 格田需要很多轮，而当前 loop 只跑到 19 次左右就结束了。对“种 43 个”这种任务，默认步数预算明显不够。

2. **进度统计疑似双计数**
   日志里 LLM 说“Planted 8 so far”，但内部 completion progress 可能已经显示到 `16/43`。这说明 `tillAndSow` 的结构化 effect 和旧的 action_result 文本回退统计可能都被算进去了。虽然这次没有导致误判成功，但会让完成判断不稳定。

所以根因不是记忆混乱，也不是缺材料，而是：

**大任务没有分批/续跑机制，planner loop 到预算上限后把“尚未完成”当成失败；同时种植进度统计还有重复计数风险。**

建议下一步修三点：

- 大任务 loop 用完时不要直接 fail，可以标记为 `needs_more_steps` / 继续同一个 ActiveTask。
- 根据 completion_spec 数量动态提高 `max_commands`，比如种 43 块田至少给 50-80 步。
- 修掉 `task_effect_count` 里结构化 effects 和旧文本 fallback 的重复计数。