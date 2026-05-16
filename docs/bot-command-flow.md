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
    F -->|action_command| H[Set planning_strategy]
    F -->|ignore/system| Z[No task]
    H --> H2[Start ActiveTask: react by default]
    H2 --> H3[Generate completion_spec]
    H3 --> H4[Infer delivery requirement]

    H4 --> I[Observe current state]
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
    J --> J2[Current task history only]
    J --> J3[Memory recall]
    J --> J4[Completion spec + active task events]
    J --> J5[Wiki/RAG context when enabled]

    J0 --> K[LLM planner]
    J1 --> K
    J2 --> K
    J3 --> K
    J4 --> K
    J5 --> K

    K --> L[Parse assistant output]
    L --> M{Has !command?}
    M -->|No| N[Send chat response]
    N --> O{Task done?}
    M -->|Yes| M1{Multiple commands produced?}
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
    AB --> AC{Matches completion_spec?}
    AC -->|Intermediate step| AF{Failure limit or max attempts?}
    AC -->|Terminal + delivery required| AD[Deliver item, then finish]
    AC -->|Terminal + no delivery| AE[Finish without item delivery]
    AC -->|No or still missing evidence| AF
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
- `InputRouter` 还会给 action task 打 `planning_strategy`，但现在默认统一使用 `react`。Minecraft 世界状态变化快，执行链路优先采用“观察 -> 执行一个命令 -> 再观察”的短循环。
- `rolling_plan` 仍保留在类型和执行保护里，方便以后做 opt-in 实验；当前路由层不会自动启用它。即使 LLM 输出多个命令，loop 也只保留第一条并在执行后重新 `observe`。
- `observe` 是规划的地基。每轮执行前后都会读取背包、实体、附近方块和基础状态，避免 LLM 只靠对话历史猜。
- `ActiveTask.completion_spec` 是任务成功边界。它在任务开始时生成，记录任务类型、目标物品、数量、可接受物品、terminal commands、intermediate commands，以及是否需要交付。
- `CommandDef.completion_kind` 只是命令级信号；最终能不能结束任务要先通过 `completion_spec`。例如 `挖8铁矿` 中 `craftRecipe("oak_planks")` 只是 intermediate，不能结束任务；只有收集到足量 `raw_iron`/`iron_ore` 后才算达成收集边界。
- 是否需要交付由 spec 的主判断和 `DispatchGate` 小模型信号联合决定。主判断权重更高，小模型只做校准；如果小模型不可用，则使用 spec 的本地判断。
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
