# Skill 写入策略

本文记录 Phase 4/5/6 的记忆与 skill 写入策略，供开发 Phase 5 文件型 skill 系统时参考。

核心原则：

- 玩家负责表达意图，输入越短越好。
- LLM 负责整理上下文、生成候选记忆或候选 skill。
- MemoryPolicy / SkillPolicy 负责最终写入决策。
- 长期记忆可以相对自动，skill 写入必须更保守。
- 文件是 truth source，索引、向量库和图结构只是加速层。

## 总体写入管线

不要让 LLM 直接拥有最终写入权。推荐流程：

```text
事件 / 玩家短指令 / 执行结果
  -> MemoryPolicy 判断类型和风险
  -> LLM 生成候选 memory / skill / task summary
  -> policy gate
  -> 自动写入 / 请求玩家确认 / 放入 review queue / 丢弃 / 只留短期
```

优先级：

```text
玩家显式短指令 > policy 自动判断 > LLM 候选建议
```

这里的“玩家显式”不是让玩家在游戏里打一大段教学文档，而是用短命令、短反馈或 yes/no 确认表达意图。bot 负责从当前上下文补全坐标、背包、执行轨迹、失败原因和证据。

## 玩家显式操作

Minecraft 聊天输入成本高，所以交互应设计成短命令。

### 短命令标记

示例：

```text
remember base
remember this chest as food
forget diamond_mine
save route mine
save skill safe tunnel
don't do that
prefer bridge
avoid night mining
```

bot 根据当前上下文补全结构化内容。例如玩家站在箱子旁说：

```text
remember this chest as food
```

bot 可以记录：

- chest 坐标和维度
- 附近标志物
- 当前箱子内容摘要
- `source=player_marked`
- `evidence=observed_nearby_chest`
- `type=world_fact/storage`

### 事后确认

bot 完成任务或生成候选 skill 后，只问短确认：

```text
Save this as safe_tunnel skill? yes/no
```

玩家只需要回复：

```text
yes
no
edit
```

### 纠错式教学

玩家不需要写完整策略，只指出问题：

```text
wrong
too risky
use bridge next time
never dig straight down
this is not base
```

LLM 将这些反馈整理成候选 feedback 或候选 skill patch。高风险行为变更默认不直接写入 skill 正文。

## Phase 4：普通记忆写入

Phase 4 的目标是文件型 memory。普通记忆可以比 skill 更自动，但必须带证据和适用边界。

适合写入：

- 玩家明确说“记住这里是基地”
- bot 已验证的事实，例如到达某坐标并确认是村庄
- 任务失败后的教训，例如没有盾牌时夜间进洞死亡
- 玩家偏好，例如以后不要破坏自然地形
- 世界事实，例如箱子位置、矿点、传送门位置

写入规则：

```text
玩家显式要求记住：高优先级写入，但仍需去重、格式化、补 evidence
bot 自动观察到事实：只有可验证事实自动写入
LLM 总结出的经验：先作为 candidate，低风险可写，高风险需确认
临时状态：只留 working memory，不写长期文件
```

普通 memory frontmatter 建议包含：

```yaml
name: base_location
type: place
source: player_marked
confidence: high
scope: world
world_id: main
evidence:
  - player said "remember base"
  - bot observed current position
ttl: null
valid_when:
  - world_id matches
invalid_when:
  - player marks a new base
```

每条长期记忆正文应尽量包含：

- What：记住什么
- Why：为什么记住，来自什么事件或证据
- How to apply：什么情况下应用
- When not to apply：什么情况下不应用

## Phase 5：skill 写入

skill 会直接影响 bot 未来行为，所以写入必须保守。LLM 可以生成草案，但不应直接修改关键执行步骤。

适合写入 skill：

- 玩家显式教学，例如“以后挖钻石就按这个流程做”
- 某个流程多次成功，结构稳定
- 同类失败模式反复出现，且修正策略稳定
- 玩家确认某个候选 skill 可保存

可以自动更新的字段：

```yaml
success_count
fail_count
confidence
last_used
last_failure
```

默认不自动改的内容：

```markdown
## 前置检查
## 执行步骤
## 失败模式
## 什么时候不适用
```

这些内容应走候选变更：

```text
LLM 生成 candidate skill patch
  -> SkillPolicy 判断风险
  -> 低风险可进入 review queue
  -> 高风险请求玩家确认
  -> 确认后写入 skill 文件
```

新 skill 创建建议满足至少一个条件：

- 玩家使用短命令 `save skill <name>`
- 同类任务成功 2-3 次以上
- 失败率低且前置条件稳定
- 该流程不是一次性场景
- ActionExecutor 可验证其中关键动作

示例 skill 写入流程：

```text
玩家：save skill safe tunnel
  -> bot 读取最近任务轨迹
  -> LLM 生成 safe_tunnel.md candidate
  -> SkillPolicy 检查是否包含危险动作、前置条件和失败模式
  -> bot 问：Save safe_tunnel skill? yes/no
  -> 玩家 yes
  -> 写入 bots/<name>/skills/safe_tunnel.md
  -> 更新 SkillIndex
```

示例 skill 文件必须包含：

- name / description
- triggers
- requires
- confidence / success_count / fail_count
- 前置检查
- 执行步骤
- 成功判定
- 失败模式
- 为什么有效
- 什么时候不适用

## Phase 6：TaskTree 写入

Phase 6 的 TaskTree 是任务复盘和晋升素材，不是直接执行系统。

适合自动写入：

- 一次复杂任务的执行摘要
- 子任务结构
- 使用过的 memory / skill
- 成功或失败原因
- learned_rules 候选

写入规则：

```text
TaskRun / task summary：自动写
learned_rules：交给 MemoryPolicy 判断是否写入 feedback
task subtree 晋升 skill：需要多次成功或玩家确认
```

示例 TaskRun：

```json
{
  "goal": "build safe starter base",
  "outcome": "success",
  "subtasks": [
    "choose location",
    "clear area",
    "build walls",
    "place bed"
  ],
  "used_memories": ["base_location", "avoid_night_caving"],
  "used_skills": ["collect_oak_logs"],
  "learned_rules": [
    "build fence before night if hostile mobs nearby"
  ]
}
```

TaskTree 中的 learned_rules 不应直接改 skill。推荐路径：

```text
learned_rule
  -> feedback memory
  -> 后续任务反复验证
  -> candidate skill patch
  -> 玩家确认或 policy 放行
```

## 风险分级

低风险，可自动写：

- 坐标、箱子位置、路径名称
- 用户偏好中的非危险项
- task summary
- skill 统计字段

中风险，建议 candidate：

- 经验教训
- 失败模式
- learned_rules
- skill 触发条件调整

高风险，需要确认：

- 修改 skill 执行步骤
- 加入可能导致死亡、丢物品、攻击玩家、破坏建筑的动作
- 绝对化规则，例如“永远不要挖矿”
- 覆盖已有高置信记忆
- 删除 skill 或长期记忆

## 推荐实现接口

```python
class WriteDecision(str, Enum):
    AUTO_WRITE = "auto_write"
    ASK_CONFIRMATION = "ask_confirmation"
    REVIEW_QUEUE = "review_queue"
    SHORT_TERM_ONLY = "short_term_only"
    DISCARD = "discard"


class WriteProposal(BaseModel):
    kind: Literal["memory", "skill", "task"]
    source: str
    confidence: float
    risk: Literal["low", "medium", "high"]
    evidence: list[str]
    target_path: str | None = None
    content: dict[str, Any] | str


class WritePolicy(Protocol):
    def decide(self, proposal: WriteProposal, state: BotState) -> WriteDecision:
        ...
```

Phase 5 开发时，建议把 `SkillPolicy` 做成 `WritePolicy` 的专门实现，复用 Phase 3 的元数据、证据和风险判断。

## 最终原则

玩家不需要写文档，只需要表达短意图。  
LLM 不是数据库管理员，而是书记员、总结员和草案生成器。  
最终写入由 policy gate 控制，越接近可执行行为，越需要保守。
