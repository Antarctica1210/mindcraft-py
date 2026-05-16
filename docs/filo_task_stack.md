可行，而且方向是对的，但我建议把它拆成两个概念：

1. **Task Stack，FILO，只执行栈顶任务**
2. **Task Snapshot/Event Log，每次状态变化追加，不覆盖**

不要真的把“每次状态更新”都当成一个新 task；更稳的是：一个 task 内部有不可变的状态历史。

**推荐形态**
```text
TaskStack [
  TaskFrame(id=xxx, message="帮我做两把铁镐")
    baseline_state
    completion_spec
    events[]
    snapshots[]
    status=running
]
```

bot 每轮只看 `TaskStack[-1]`，也就是最新任务。任务完成、失败、取消时 pop 或 archive；如果是明确打断型新命令，就 push 新 task，并把旧 task 标记 `interrupted/cancelled`。

**好处**

- **任务隔离更强**：旧任务不会继续影响新任务，能减少“多个 task 同时生效”的感觉。
- **完成判断更可靠**：可以拿 `baseline_state` 和当前状态做 delta，比如开始时铁镐 0，把任务完成条件定为 `iron_pickaxe >= baseline + 2` 或 `delivered iron_pickaxe x2`。
- **调试非常清楚**：每次状态更新都是 append，不覆盖。日志能还原“为什么 bot 认为完成”。
- **更像事务**：任务开始时冻结目标和基线，结束时必须通过 completion_spec 验证，不能因为 `digDown` 这种中间动作误判成功。
- **支持中断/恢复**：例如玩家临时说 `come to me`，可以 push 一个导航任务；完成后 pop，再决定是否恢复上一个任务。

**坏处 / 风险**

- **FILO 不能解决所有完成边界**：只比较第一个状态不够。比如交付任务完成后，bot 背包里的物品会减少，所以不能只看 inventory delta，还要看 `givePlayer` 成功、是否先靠近玩家、物品是否真的减少。
- **状态快照会变多**：如果每一步 observe 都存完整状态，日志和内存会膨胀。可以只存关键字段或 delta。
- **外部世界变化会污染对比**：玩家捡走物品、怪物移动、其他实体改变世界，都可能让 baseline/current diff 不纯粹。
- **任务嵌套语义要小心**：`帮我做铁镐` 中途需要 `做石镐`，这不一定应该 push 成新用户任务，更适合当作同一 task 的 subgoal/dependency。
- **清空列表不要丢历史**：完成时可以清空 active stack，但要 archive 到 task history/memory，否则调试信息没了。

我会建议这样落地：

- `ActiveTaskStack`：只执行 top task。
- `TaskFrame.baseline_state`：任务开始时保存 inventory、position、nearby player distance、关键 world evidence。
- `TaskFrame.snapshots/events`：每次 action 后 append delta。
- `completion_spec` 决定完成方式：
  - craft：目标物品数量达到 baseline + requested，或已交付 requested。
  - collect：目标物品达到 baseline + requested，或已交付 requested。
  - delivery：必须 `goToPlayer` 成功后 `givePlayer` 成功。
  - world_change：必须有专门 world evidence，不能只凭任意 `world_change` 命令。
- 任务结束时 archive frame，然后清空/弹出。

结论：**可行，而且值得做**。但核心不是“FILO 列表本身”，而是“栈顶任务独占执行 + immutable snapshots + baseline/completion_spec 联合判断”。这样能直接解决你现在遇到的误完成、任务串扰、交付没到身边这几类问题。

## Phase 1 实现状态

已落地第一版“栈顶任务独占 + baseline 快照”：

- `Agent` 现在维护 `_active_task_stack`，新玩家任务会取消旧任务，并只保留最新任务作为栈顶 active frame。
- `ActiveTask` 开始时记录 `baseline_inventory`、`baseline_position` 和第一条 `baseline` snapshot。
- 每次命令成功后追加一条 compact snapshot，方便复盘“bot 为什么认为任务进展了”。
- `completion_spec` 的物品类完成判断改为优先比较 `current - baseline`，例如开始时已有 1 把铁镐，`帮我做两把铁镐` 必须新增 2 把，而不是看到背包里有铁镐就完成。
- `digDown` 从默认终止命令降级为条件终止命令，只有玩家明确要求向下挖时才允许完成任务，避免“做铁镐/挖铁矿”被中间探索动作误判成功。
- 任务结束后会清空 active stack，但历史仍通过 task events、summary 和 memory quality gate 做归档或跳过。

后续可以继续增强：

- 将 snapshot 写入独立 task archive 文件，避免只在内存中可见。
- 对 delivery 也引入 baseline/delta，例如记录 `givePlayer` 前后的距离、物品减少量和 give result。
- 支持真正的 subgoal stack：玩家任务仍是栈顶主任务，`做石镐` 这类依赖作为 task 内部 subgoal，而不是新用户任务。
