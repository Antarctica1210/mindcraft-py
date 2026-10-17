# P0: 动作-状态转移图

## 动机

当前系统每次执行任务都需要 LLM 从头规划，即使已经执行过数百次相同的操作序列（如「砍树→合成木板→做工作台」）。高频重复路径浪费 token 且增加延迟。

## 设计

```
                    ┌──────────────────────┐
                    │    State: 无木材      │
                    │   nearby: oak_tree   │
                    └──────┬─────────────┘
                           │ !collectBlock("oak_log",5)
                           │ weight: 0.95 (5次成功/0次失败)
                           ▼
                    ┌──────────────────────┐
                    │   State: 有木材       │
                    │   inv: oak_log x5    │
                    └──┬───────────────┬───┘
                       │               │
        !craftRecipe   │               │  !placeBlock("oak_log")
        ("oak_planks") │               │
        weight: 0.9    │               │ weight: 0.5
                       ▼               ▼
              ┌──────────────┐  ┌──────────────┐
              │ 有橡木木板    │  │ 放置了原木     │
              └──────────────┘  └──────────────┘
```

## 数据结构

```javascript
// 状态节点
class StateNode {
  constructor() {
    this.id = "state_hash";
    this.conditions = {
      inventory: { "oak_log": ">=5" },
      nearby: ["oak_tree"],
      position: null         // null 表示通配
    };
    this.embedding = [...];  // 状态摘要向量
  }
}

// 动作边
class ActionEdge {
  constructor() {
    this.from = "state_A";
    this.to = "state_B";
    this.command = "!collectBlock";  // 或 null 表示 compound action
    this.args = ["oak_log", 5];
    this.success_count = 5;
    this.fail_count = 0;
    this.avg_duration_ms = 12000;
    this.preconditions = ["has_axe"]; // 前置条件
    this.side_effects = ["axe耐久-1"]; // 副作用
  }
}
```

## 运行时流程

```
1. 当前状态序列化 → 计算 embedding
2. 在图数据库中找到最相似的状态节点
3. 从该节点出发的所有边，按 success_rate 排序
4. 若最高权重边超过阈值 → 直接执行，跳过 LLM
5. 若低于阈值或需探索 → 正常走 LLM 路径
6. 执行动作 → 到达新状态 → 更新权重
```

## 关键模块改动

| 模块 | 改动 |
|------|------|
| `agent/action_manager.js` | 新增图查询与直接执行路径 |
| `agent/history.js` | 新增 StateNode 序列化逻辑 |
| `agent/library/world.js` | 新增 状态特征提取（库存摘要、附近实体等） |
| 新文件 `agent/graph_store.js` | 图存储与检索（可用内存 + JSON 文件持久化） |

## 权重衰减与强化

- 成功：`success_count++`，`weight = success_count / (success_count + fail_count)`
- 失败：`fail_count++`，权重随之下跌
- 遗忘因子：每次更新时 `success_count *= 0.99`，旧经验随时间淡出

## 收益

- 高频动作序列无需 LLM 参与，延迟从秒级降到毫秒级
- 失败路径自动边缘化
- 可在不同 Minecraft 世界中泛化（state 通配 position 等动态维度）
