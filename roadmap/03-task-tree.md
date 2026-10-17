# P2: 树形任务记忆

## 动机

当前系统没有对复杂任务的分解结构进行持久化。每次「砍树→合成→建造」这样的多步任务都需要 LLM 从零规划。如果能将成功完成的任务结构和执行经验存储为树，可以直接召回子任务子树。

## 设计

```
                     TaskTree
                        │
         ┌──────────────┼──────────────┐
      build_house    mining     farming
         │               │           │
    ┌────┴────┐      diamond    wheat_farm
  gather   construct     │
  wood       house    [subtree...]
    │           │
  [步骤1-5]  [步骤6-10]
```

## 数据结构

```javascript
{
  id: "task_build_house_001",
  goal: "build a wooden house",
  status: "completed",          // pending | in_progress | completed | failed
  created_at: "2026-05-08T10:00:00Z",
  completed_at: "2026-05-08T10:15:00Z",
  duration_ms: 900000,

  // 子任务
  subtasks: [
    {
      id: "sub_gather_wood",
      goal: "gather enough wood",
      plan: [
        "!collectBlock('oak_log', 10)",
        "!craftRecipe('oak_planks', 4)"
      ],
      status: "completed",
      dependencies: []            // 依赖的其他子任务 id
    },
    {
      id: "sub_build_walls",
      goal: "build 4 walls",
      plan: [
        "!placeBlock('oak_planks', x, y, z)",
        // ...
      ],
      status: "completed",
      dependencies: ["sub_gather_wood", "sub_craft_table"]
    }
  ],

  // 执行记录
  executions: [
    {
      id: "exec_001",
      action: "!collectBlock('oak_log', 10)",
      result: "Collected 10 oak logs",
      duration_ms: 45000,
      outcome: "success"
    }
  ],

  // 学到的规则
  learned_rules: [
    "oak_planks requires oak_log (4:1 ratio)",
    "crafting_table requires 4 oak_planks",
    "use axe for faster wood gathering",
    "house should be built on flat ground"
  ]
}
```

## 运行时使用

```
1. 用户说 "build a stone house"
        ↓
2. 在 TaskTree 中搜索相关任务
   → 找到 "build_wooden_house" (相似度 0.85)
        ↓
3. 提取该任务的 subtask 结构作为模板
   → gather_material, craft, build_walls, build_roof
        ↓
4. 将模板注入 LLM prompt
   → LLM 只需做材质替换 (wood → stone)，无需重新规划结构
        ↓
5. 执行中记录每个子任务的成功/失败
   → 更新树的权重
```

## 树的持久化与检索

```javascript
// 存储
bots/andy/tasks/
  ├── task_build_house_001.json
  ├── task_mining_diamond_001.json
  └── task_index.json            // 元数据索引

// 检索策略
// 1. 关键词过滤：goal 包含 "build", "house"
// 2. embedding 排序：当前对话 vs 各任务的 goal + learned_rules
// 3. 取 top-3，注入 LLM prompt 作为参考
```

## 关键模块改动

| 模块 | 改动 |
|------|------|
| `agent/tasks/tasks.js` | 新增树结构持久化与检索 |
| `agent/history.js` | 新增当前任务节点上下文注入 |
| 新文件 `agent/task_tree.js` | 树存储、检索、模板匹配 |

## 收益

- 复杂任务可复用子树结构，免去重复规划
- `learned_rules` 记录了每个子任务的领域知识
- 相似任务的差异只需 LLM 做材质/参数替换，降低认知负担
