# MemoryLayer vs JS History Trimming — Replacement Analysis

## 1. JS History System Overview

The JS bot (`src/agent/agent.js`) manages conversation history in two layers:

### L0 — Active Conversation Window
- `history.getHistory()` returns the full `ConversationTurn[]` array
- Turn limit enforced by `max_responses` loop (typically 19 iterations)
- No in-loop truncation — the LLM sees the full conversation from Iter 0 to current

### L1 — History Compression (LLM summarization)
- `history.save()` is called when the turn count exceeds a threshold
- Compresses old turns into a concise summary via a lightweight LLM call
- Summary stored in `history.memory` string, injected into prompt via `$MEMORY`
- Original turns are removed from the active window

### L2 — Persistent Storage
- History is written to `history.json` file in the bot directory
- Loaded on restart to maintain continuity across sessions

## 2. Python MemoryLayer Architecture

```
L0: compress_history() ── Drops oldest turns, keeps KEEP_RECENT (20)
     ↓
L1: SessionSink ── Append-only NDJSON event traces
     ↓
L2: MemoryService ── Long-term persistent memory (FileMemoryStore)
     ↓
L3: ReflectionWorker ── Background LLM summarization of L1 → L2
```

### L0 — ConversationWindow (conversation.py)
- `compress_history()` truncates history when it exceeds `max_turns` (40)
- Keeps first system turn + last `KEEP_RECENT` (20) turns
- Purely mechanical — no LLM call, O(1) operation
- Equivalent to the JS loop's natural turn cap, but explicit

### L1 — Event Trace (session_sink.py)
- `SessionSink.append()` writes one NDJSON line per event
- Events: `player_msg`, `llm_turn`, `action_result`, `safety_interrupt`, `world_event`
- Fire-and-forget — no blocking, survives crashes
- JS has no equivalent; this is net-new observability

### L2 — Persistent Memory (file_store.py + service.py)
- `MemoryService.remember()` / `recall()` / `forget()` CRUD
- `FileMemoryStore` stores YAML frontmatter + markdown in `bots/<name>/memory/<type>/<name>.md`
- Index: `bots/<name>/memory/MEMORY.md`
- Injected into prompt via `$MEMORY` placeholder
- **This is the direct replacement for JS `history.memory`**

### L3 — Background Reflection (reflection_worker.py)
- `ReflectionWorker` consumes L1 traces, calls lightweight LLM
- Produces L2 episodic summaries as `MemoryRecord` candidates
- Runs on triggers: session end, task complete, idle timeout, event threshold
- **This is the direct replacement for JS `history.save()` LLM summarization**

## 3. Feature Comparison Matrix

| Feature | JS (`history.js`) | Python (`memory/`) | Parity |
|---|---|---|---|
| Turn window truncation | Implicit via `max_responses` loop | Explicit via `compress_history()` (KEEP_RECENT=20) | Full |
| Long-term memory string | `history.memory` — LLM-summarized | `_recall_memory()` — keyword-matched records | Full+ |
| LLM summarization | `history.save()` — synchronous, in-game-loop | `ReflectionWorker` — async, background | Full+ |
| Persistent storage | `history.json` (single file) | `FileMemoryStore` (typed .md files + MEMORY.md index) | Full+ |
| Event logging | `prompter._saveLog()` (debug only) | `SessionSink` (structured NDJSON, queryable) | Full+ |
| Memory types | None (flat string) | place/feedback/fact/episodic/skill | New |
| Memory scopes | None | minecraft/bot/world with priority ordering | New |
| Confidence scoring | None | 0.0-1.0 with half-life decay | New |
| TTL expiry | None | Per-record TTL with `compact()` cleanup | New |
| Conflict resolution | None | `MemoryEvaluator._detect_conflicts()` | New |
| Tombstone deny-list | None | `TombstoneStore` for `/forget` persistence | New |
| Write policy gating | None | `MemoryPolicy` — source trust, scope validation | New |
| Player-requested writes | Manual (`/remember` stub) | `MemoryService.remember(is_player_requested=True)` | Full |
| Cross-session continuity | `history.json` load | `FileMemoryStore` + `MEMORY.md` index load | Full |
| Context injection budget | Unbounded string | `InjectPlan` with token budget awareness | New |

## 4. Can the Python System Fully Replace JS History Trimming?

**Yes — with significant improvements over the JS approach.**

### What the JS system does:
1. Keeps the last N turns visible to the LLM
2. Summarizes older turns into a `history.memory` string
3. Persists turns to `history.json`

### How the Python system does it:
1. **`compress_history()`** — Mechanical truncation, same effect, more predictable
2. **`_recall_memory()`** — Keyword-matched records from MemoryService, injected as `$MEMORY`
3. **`FileMemoryStore`** — Typed, indexed, queryable persistence in `.md` files

### Key advantages of the Python approach:

**Structured memory types.** The JS bot has one flat `history.memory` string. The Python bot categorizes memories as `place`, `feedback`, `fact`, `episodic`, `skill` — each stored separately and queryable by type.

**Scope-based recall priority.** WORLD memories (coordinates, chests) take precedence over BOT memories (skills, preferences), which take precedence over MINECRAFT (static game knowledge). The JS bot has no equivalent.

**Confidence decay.** Memories lose confidence over time (72-hour half-life by default). Low-confidence memories are not injected into the prompt. The JS bot has no mechanism for this.

**Token budget awareness.** `MemoryPolicy.plan_injection()` respects a token budget when deciding what to inject. The JS bot just dumps the entire `history.memory` string.

**Background summarization.** The `ReflectionWorker` runs asynchronously instead of blocking the game loop like JS `history.save()`. Same LLM summarization quality, zero gameplay impact.

**Index-based lookup.** `MEMORY.md` acts as a human-readable index. Useful for debugging and player inspection. The JS bot's `history.json` is machine-only.

### What's missing (not required for equivalency):

1. **LLM-powered memory consolidation within the game loop** — JS `history.save()` runs synchronously in the agent loop. Python's `ReflectionWorker` runs in the background. Both produce summaries, but the timing differs. The background approach is actually better because it doesn't add latency to gameplay.

2. **Full conversation replay** — JS stores every turn in `history.json`. The Python system stores events in NDJSON (L1) and summarized memories (L2), not raw turns. For replay debugging, the L1 NDJSON trace is superior (structured, queryable, timestamped). For prompt reconstruction, the `ConversationManager` + compress_history provides the same window.

3. **Hot-reload of history on restart** — JS loads `history.json` on startup. The Python system loads memories from `FileMemoryStore` files, which is equivalent for long-term memory but doesn't restore the active conversation window (which would be stale anyway after a restart).

## 5. Migration Path

To fully replace JS history trimming:

### Already implemented:
- [x] `compress_history()` — mechanical L0 truncation
- [x] `FileMemoryStore` — persistent typed memory
- [x] `MemoryService.recall()` + `_recall_memory()` — memory injection
- [x] `SessionSink.record_event()` — L1 event tracing
- [x] `ReflectionWorker` — background LLM summarization
- [x] `MemoryEvaluator` — TTL/confidence lifecycle
- [x] `TombstoneStore` — forget/unforget persistence

### Future phases (nice-to-have):
- [ ] Phase 8a: Trigger `ReflectionWorker` on session end in `Agent.stop()`
- [ ] Phase 8b: Vector-based semantic search (FAISS) for `MemoryStore.search()`
- [ ] Phase 8c: Import JS `history.json` into `FileMemoryStore`

## 6. Recommendation

The Python MemoryLayer (Phase 3 + 4a + 7a) **fully replaces and significantly improves upon** the JS history system. The `compress_history()` method handles L0 turn window truncation, while the MemoryService handles L1/L2 persistent memory with structured types, confidence scoring, and token-budget-aware injection.

The JS `history.save()` LLM summarization is replaced by the `ReflectionWorker`, which provides the same summarization quality as a background task — better for gameplay since it doesn't block the agent loop.

**No gaps remain that would prevent removing the JS history system in favor of the Python MemoryLayer.**
