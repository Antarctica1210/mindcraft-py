# Bot Command Flow

这张图描述一次玩家命令从 Minecraft 聊天进入，到 Python agent 规划、执行、观察、结束任务并写入记忆的完整链路。

```mermaid
flowchart TD
    A[Player chat message] --> B[Socket server emits chat event]
    B --> C[SocketBridge dispatches event to Python]
    C --> D[Agent.handle_message sender/message]

    D --> E[InputRouter keyword/regex classifier]
    E --> F{Intent}
    F -->|chitchat| G[Prompter replies as conversation]
    F -->|action_command| H[Choose planning_strategy]
    F -->|ignore/system| Z[No task]
    H --> H1{Strategy}
    H1 -->|react| H2[Start ActiveTask: immediate next step only]
    H1 -->|rolling_plan| H3[Start ActiveTask: 2-3 step short horizon]

    H2 --> I[Observe current state]
    H3 --> I
    I --> I1[Inventory]
    I --> I2[Entities]
    I --> I3[Nearby blocks]
    I --> I4[Position/health/food/goal]

    I1 --> J[Build plan prompt]
    I2 --> J
    I3 --> J
    I4 --> J
    J --> J0[Strategy prompt]
    J --> J1[Command docs + completion_kind]
    J --> J2[Conversation/task history]
    J --> J3[Memory recall]
    J --> J4[Wiki/RAG context when enabled]

    J0 --> K[LLM planner]
    J1 --> K
    J2 --> K
    J3 --> K
    J4 --> K

    K --> L[Parse assistant output]
    L --> M{Has !command?}
    M -->|No| N[Send chat response]
    N --> O{Task done?}
    M -->|Yes| M1{rolling_plan produced multiple commands?}
    M1 -->|Yes| M2[Keep first command, ignore later commands]
    M1 -->|No| P[ActionExecutor.execute]
    M2 --> P

    P --> Q[Normalize args/kwargs]
    Q --> R{Command kind}
    R -->|Built-in command| S[SocketBridge JSON request]
    R -->|newAction| T[Generate dynamic JS code]
    T --> U[SocketBridge execute_code request]

    S --> V[socket_server.js handler]
    U --> V
    V --> W[Mineflayer/pathfinder/world API]
    W --> X[Minecraft world changes]
    X --> Y[JSON response/result]

    Y --> AA[Record ActiveTask action event]
    AA --> AB[Post-action observe]
    AB --> AC{Command success + state evidence?}
    AC -->|Yes, delivery task| AD[Finish after givePlayer/delivery]
    AC -->|Yes, world/custom task| AE[Finish without item delivery]
    AC -->|No or still missing evidence| AF{Failure limit or max attempts?}
    AF -->|No| J
    AF -->|Yes| AG[Finish failed]

    AD --> AH[Summarize ActiveTask]
    AE --> AH
    AG --> AH
    AH --> AI[Write episodic memory]
    AI --> AJ[Loop returns to idle/run_loop modes]
```

## Key Points

- `InputRouter` 决定一句话是否应该进入执行链路；`DispatchGate` 可作为 LLM 分类器补充，但不可用时会回退到关键词路由。
- `InputRouter` 还会给 action task 打 `planning_strategy`：`react` 用于战斗、跟随、搜索、停止等高动态任务；`rolling_plan` 用于制作、烧炼、挖矿、种植、养动物等稳定流程。
- `rolling_plan` 不是 full plan。LLM 可以提前想 2-3 步，但只有第一条命令会执行；未来步骤必须是普通文字，执行后重新 `observe` 再决定是否保留或改写。
- `observe` 是规划的地基。每轮执行前后都会读取背包、实体、附近方块和基础状态，避免 LLM 只靠对话历史猜。
- `CommandDef.completion_kind` 告诉 loop 怎么判断结束：例如 `delivery` 依赖交付，`world_change`/`custom_action` 可以用状态证据结束，`query`/`navigation` 默认不直接完成任务。
- `newAction` 仍然通过 `socket_server.js` 执行，但只能使用动态 API 文档暴露的 `api.*` 方法，不应假设原生 Mindcraft 的 `skills.*` 对象存在。
- 任务结束后会写入 episodic memory；失败任务也会记录原因，后续 prompt 可引用这些经验。

## Minimal Runtime Boundary

当前 Python 版本只需要一层很薄的 Node/Mineflayer 桥：

```mermaid
flowchart LR
    Python[Python agent/core/executor] --> Bridge[SocketBridge TCP JSON]
    Bridge --> Node[src/mc/socket_server.js]
    Node --> Data[src/utils/mcdata.js]
    Node --> Mineflayer[Mineflayer plugins]
    Mineflayer --> MC[Minecraft server]
```

原生 Mindcraft 的 JS agent、models、process、browser UI、profile runner 已不在主执行链路里。
