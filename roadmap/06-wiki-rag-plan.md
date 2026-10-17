# Wiki RAG 实施方案

> **分支**: `refactor/stage-3-rag`
> **前提**: Phase 1/2 agent 核心已完成，`searchWiki` 已有 HTTP 实现（`executor._search_wiki`）

---

## 动机

当前 `searchWiki` 每次都向 `minecraft.wiki` 发起 HTTP 请求，存在以下问题：

| 问题 | 影响 |
|---|---|
| 网络延迟 100–2000ms | LLM plan 循环被拖慢 |
| HTML 粗暴 regex 剥离 | 大量噪音/导航文本进入上下文 |
| 无语义匹配 | 查 "bed recipe" 只能精确 URL，不能模糊检索 |
| 中文查询无效 | bot 用中文内部查询时完全匹配失败 |
| 频繁调用触发 loop guard | 模型 spam searchWiki 时 F1/F3 快速熔断 |
| 离线不可用 | 局域网服务器场景无法联网 |

解决方案：**离线预建混合索引**（BGE-M3 dense + BGE-M3 sparse）→ Reciprocal Rank Fusion → 交叉编码器重排 → HTTP 兜底。

---

## 检索模型选型

### 为什么选 `BAAI/bge-m3` + `BAAI/bge-reranker-v2-m3`

| 维度 | `all-MiniLM-L6-v2`（原方案） | `BAAI/bge-m3`（新方案） |
|---|---|---|
| 语言支持 | 英文为主 | 100+ 语言，中文原生支持 |
| 向量维度 | 384 | 1024 |
| 稀疏向量 | ✗（需单独 BM25） | ✓ 内置 lexical weights 输出 |
| 检索质量 (BEIR) | 42.7 | 54.9 |
| 模型大小 | ~90MB | ~570MB（FP16 ~285MB） |

**关键优势**：BGE-M3 单次 forward pass 同时输出 dense 向量和 sparse lexical weights，无需维护独立 BM25 索引，两种检索路径来自同一个模型。

`bge-reranker-v2-m3` 是 BGE-M3 的配套交叉编码器（XLM-RoBERTa-Large 架构微调），对 `[query, chunk]` 对打分，质量显著高于双编码器相似度。

---

## 整体架构

```
┌──────────────────────────────────────────────────────────┐
│  scripts/ingest_wiki.py  (离线一次性执行)                  │
│                                                          │
│  MediaWiki API                                           │
│    └─ fetch category members + page wikitext            │
│         │                                               │
│         ▼                                               │
│  mwparserfromhell → 纯文本 → chunk_text()               │
│  400词/块, 50词重叠                                      │
│         │                                               │
│         ▼                                               │
│  BGEM3FlagModel.encode(                                 │
│      return_dense=True,                                 │
│      return_sparse=True                                 │
│  )                                                      │
│         │                                               │
│         ├─── dense_vecs (N×1024 float32)                │
│         │      └─ faiss.IndexFlatIP → index.faiss       │
│         │                                               │
│         └─── lexical_weights [{token: weight}, ...]     │
│                └─ scipy CSR matrix → sparse.npz         │
│                └─ vocab map → vocab.json                │
│                                                         │
│  data/wiki/                                             │
│    ├── index.faiss      # dense FAISS 索引              │
│    ├── sparse.npz       # BGE-M3 sparse 向量 (CSR)      │
│    ├── vocab.json       # token → column 映射           │
│    ├── chunks.jsonl     # 块元数据 (title/url/text)      │
│    └── manifest.json    # 模型名/维度/块数               │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  src/knowledge/wiki_store.py  (运行时)                    │
│                                                          │
│  WikiVectorStore.load()                                  │
│    ├─ BGEM3FlagModel('BAAI/bge-m3')                     │
│    ├─ FlagReranker('BAAI/bge-reranker-v2-m3')           │
│    ├─ faiss.read_index('index.faiss')                   │
│    ├─ scipy.sparse.load_npz('sparse.npz')               │
│    └─ chunks.jsonl → list[WikiChunk]                    │
│                                                          │
│  WikiVectorStore.search(query, k=5)                      │
│    │                                                     │
│    ├─ encode(query, dense=True, sparse=True)            │
│    │                                                     │
│    ├─ [Dense path]                                      │
│    │    └─ FAISS.search(dense_vec, top_50)              │
│    │         → ranked_dense: [(chunk_id, score), ...]   │
│    │                                                     │
│    ├─ [Sparse path]                                     │
│    │    └─ CSR.dot(sparse_vec) → top_50                 │
│    │         → ranked_sparse: [(chunk_id, score), ...]  │
│    │                                                     │
│    ├─ [RRF Fusion]                                      │
│    │    └─ merge top_50+top_50 → top_20 candidates      │
│    │                                                     │
│    └─ [Reranker]                                        │
│         └─ bge-reranker-v2-m3.compute_score(            │
│                [(query, chunk.text) × 20]               │
│            ) → sort → top_k WikiChunk[]                 │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  src/agent/executor.py  _search_wiki()                   │
│                                                          │
│  if store.loaded → store.search(query, k=5)             │
│    └─ format chunks → return text                       │
│  else → HTTP fallback (现有逻辑)                         │
└──────────────────────────────────────────────────────────┘
```

---

## 数据源

### Minecraft Wiki

- **URL**: `https://minecraft.wiki`
- **API**: MediaWiki JSON API (`/api.php`)
- **License**: CC BY-NC-SA 3.0

### 抓取策略

| 类型 | API 参数 | 说明 |
|---|---|---|
| 分类成员 | `action=query&list=categorymembers&cmtitle=Category:X&cmlimit=500` | 批量获取标题 |
| 页面原文 | `action=query&prop=revisions&rvprop=content&rvslots=main` | 原始 wikitext |

请求间隔 ≥ 300ms，礼貌爬取，不触发 wiki 限速。

### 目标分类

```
Crafting          # 合成配方与机制
Blocks            # 方块属性与挖掘
Items             # 物品用途
Mobs              # 生物行为与战斗
Biomes            # 生物群系
Food              # 食物与饥饿值
Enchantments      # 附魔
Potions           # 药水与酿造
Status_effects    # 状态效果
Mechanics         # 游戏机制（农业、钓鱼等）
Structures        # 生成结构
```

### 高优先级固定页面

```
Crafting, Smelting, Brewing, Enchanting, Fishing, Farming, Mining,
Trading, Inventory, Experience, Hunger, Health, Damage,
Armor, Shield, Sword, Pickaxe, Axe, Shovel, Hoe, Bow, Crossbow,
Bed, Chest, Furnace, Crafting_table, The_Nether, The_End, Overworld
```

---

## 文本处理

### wikitext → 纯文本

```python
import mwparserfromhell
wikicode = mwparserfromhell.parse(wikitext)
text = wikicode.strip_code(normalize=True, collapse=True)
```

备用（未安装时）：正则剥离 `{{...}}`、`[[...]]`、`''`、`<tag>`。

### 分块策略

```
chunk_size    = 400 词
chunk_overlap = 50  词
min_words     = 30  词（过滤 stub 页面）
分割优先级     = 段落换行 > 句号 > 词边界
```

每块元数据：`title`, `url`, `chunk_id`。`chunk_id` 与 FAISS/CSR 行号严格对齐。

---

## 向量化与索引

### BGE-M3 双路径编码

BGE-M3 一次 forward pass 同时产出两种向量：

```python
from FlagEmbedding import BGEM3FlagModel

model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)

# 建索引时（批量）
output = model.encode(
    texts,
    batch_size=12,
    return_dense=True,
    return_sparse=True,
    return_colbert_vecs=False,  # 不使用 ColBERT，节省存储
)
dense_vecs = output['dense_vecs']        # np.ndarray (N, 1024) float32
lexical_ws = output['lexical_weights']   # list[dict[str, float]]
```

### Dense 索引

```python
import faiss, numpy as np

# 归一化 → 内积 = 余弦相似度
faiss.normalize_L2(dense_vecs)
index = faiss.IndexFlatIP(1024)
index.add(dense_vecs)
faiss.write_index(index, "data/wiki/index.faiss")
```

### Sparse 索引

BGE-M3 的 lexical_weights 是 token → float 的稀疏映射。存储为 scipy CSR 矩阵：

```python
import scipy.sparse as sp

# 建立 token → column 映射（vocab.json）
all_tokens = sorted({t for lw in lexical_ws for t in lw})
tok2col = {t: i for i, t in enumerate(all_tokens)}

rows, cols, vals = [], [], []
for row_id, lw in enumerate(lexical_ws):
    for tok, w in lw.items():
        rows.append(row_id)
        cols.append(tok2col[tok])
        vals.append(w)

mat = sp.csr_matrix(
    (vals, (rows, cols)),
    shape=(len(lexical_ws), len(tok2col)),
    dtype=np.float32,
)
sp.save_npz("data/wiki/sparse.npz", mat)
```

查询时：同样对 query 取 lexical_weights → 投影到同一 vocab → 与 CSR 矩阵做点积。

---

## 检索与重排流程

### 1. Reciprocal Rank Fusion (RRF)

从 dense top-50 和 sparse top-50 中合并唯一候选，用 RRF 公式打分：

```
RRF_score(d) = Σ  1 / (k + rank_i(d))    k = 60
```

取 RRF 分数最高的 **top-20** 作为重排候选集。

### 2. 交叉编码器重排

```python
from FlagEmbedding import FlagReranker

reranker = FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=True)

pairs = [(query, chunk.text) for chunk in candidates_20]
scores = reranker.compute_score(pairs, normalize=True)
# normalize=True → 输出 [0, 1]，便于设置最低分阈值

ranked = sorted(zip(scores, candidates_20), reverse=True)
top_k = [chunk for _, chunk in ranked[:k]]
```

重排器对 query+chunk 联合编码，能捕捉双编码器独立编码时丢失的交叉语义。

### 延迟预估

| 阶段 | CPU（无 GPU） | Apple M 系列 MPS | CUDA GPU |
|---|---|---|---|
| BGE-M3 query 编码 | ~80ms | ~20ms | ~10ms |
| FAISS dense search | <5ms | <5ms | <1ms |
| CSR sparse dot product | ~10ms | ~10ms | ~5ms |
| RRF 融合 | <1ms | <1ms | <1ms |
| Reranker 20 候选 | ~500ms | ~80ms | ~40ms |
| **总计** | **~600ms** | **~115ms** | **~56ms** |

CPU 总延迟约 600ms，仍优于 HTTP（500–2000ms + 噪音 + 无语义）。若在 Apple Silicon 或有 GPU 的机器上运行，体验接近即时。

---

## 文件结构

```
mindcraft-py/
├── scripts/
│   └── ingest_wiki.py          # 离线建索引脚本
├── src/
│   └── knowledge/
│       ├── __init__.py         # 导出 WikiVectorStore
│       ├── knowledge_graph.py  # 已有（结构化游戏数据）
│       └── wiki_store.py       # 新增：混合检索 + 重排
├── data/
│   └── wiki/                   # 由 ingest_wiki.py 生成，git-ignored
│       ├── index.faiss         # BGE-M3 dense 向量
│       ├── sparse.npz          # BGE-M3 sparse 向量 (CSR)
│       ├── vocab.json          # token → CSR column 映射
│       ├── chunks.jsonl        # 块元数据 (title/url/text/chunk_id)
│       └── manifest.json       # 模型名/维度/块数/建立时间
└── roadmap/
    └── 06-wiki-rag-plan.md
```

---

## 核心类设计

### `WikiChunk`

```python
@dataclass(frozen=True)
class WikiChunk:
    title: str       # 页面标题，如 "Crafting"
    url: str         # 完整 wiki URL
    text: str        # 该块纯文本
    chunk_id: int    # FAISS/CSR 行号（严格对齐）
```

### `WikiVectorStore`

```python
class WikiVectorStore:
    def __init__(
        self,
        index_dir: str | Path = "data/wiki",
        embed_model: str = "BAAI/bge-m3",
        rerank_model: str = "BAAI/bge-reranker-v2-m3",
        use_fp16: bool = True,
        rerank_candidates: int = 20,  # RRF → reranker 的候选数
        dense_candidates: int = 50,   # 每路检索取 top-N
    ) -> None: ...

    def load(self) -> bool:
        """加载所有索引 + 两个模型。任何依赖缺失时静默返回 False。"""

    def search(self, query: str, k: int = 5) -> list[WikiChunk]:
        """
        1. encode query (dense + sparse)
        2. FAISS top-dense_candidates
        3. CSR sparse top-dense_candidates
        4. RRF → top-rerank_candidates
        5. bge-reranker-v2-m3 → top-k
        store 未加载时返回 []。
        """

    @property
    def loaded(self) -> bool: ...
```

### `executor._search_wiki` 改造

```python
async def _search_wiki(self, _bridge: Any, query: str) -> str:
    # 优先本地混合检索
    if self._wiki_store and self._wiki_store.loaded:
        chunks = self._wiki_store.search(query, k=5)
        if chunks:
            parts = [f"## {c.title}\n{c.text}" for c in chunks]
            logger.info("searchWiki: local hit (%d chunks)", len(chunks))
            return "\n\n---\n\n".join(parts)

    # 兜底：原有 HTTP 实现
    logger.info("searchWiki: fallback to HTTP")
    return await _search_wiki_http(query)
```

---

## 依赖

```toml
# pyproject.toml 新增
FlagEmbedding = ">=1.2"       # BGEM3FlagModel + FlagReranker
faiss-cpu = ">=1.7"           # 换 faiss-gpu 接口相同
scipy = ">=1.11"              # sparse CSR 矩阵
mwparserfromhell = ">=0.6"    # wikitext 解析
```

> **注意**：`FlagEmbedding` 依赖 `torch`（CPU 版本约 800MB）。
> BGE-M3 模型权重约 570MB（FP16 ~285MB），bge-reranker-v2-m3 同量级。
> 首次运行时 HuggingFace Hub 自动下载。
> GPU 加速：安装 CUDA 版 torch 后两个模型自动使用 GPU，无需代码改动。

---

## 实施阶段

### Stage 3a — 建索引脚本

**目标**: `python scripts/ingest_wiki.py --max-pages 50` 产出完整 `data/wiki/`

任务清单：
- [x] MediaWiki API 抓取（分类成员 + 页面 wikitext）
- [x] mwparserfromhell 解析 + 段落优先分块
- [x] BGEM3FlagModel 批量编码（dense + sparse）
- [x] FAISS IndexFlatIP 写入 `index.faiss`
- [x] 构建 vocab.json + CSR 矩阵写入 `sparse.npz`
- [x] chunks.jsonl + manifest.json 写入
- [x] CLI 参数：`--max-pages`、`--categories`、`--output`、`--fp16/--fp32`
- [x] Apple Silicon `--device auto` 自动优先选择 MPS

---

### Stage 3b — WikiVectorStore（混合检索 + 重排）

**目标**: `store.search("床的配方", k=5)` 返回正确中文语义命中

任务清单：
- [x] `src/knowledge/wiki_store.py`：`WikiChunk`、`WikiVectorStore` 完整实现
- [x] Dense 路径：FAISS.search
- [x] Sparse 路径：vocab 投影 + CSR 点积 top-N
- [x] RRF 融合逻辑（k=60）
- [x] FlagReranker 重排 + normalize=True 阈值过滤（< 0.1 的块丢弃）
- [x] 所有模型/索引缺失时静默 fallback，不影响 bot 启动
- [x] `src/knowledge/__init__.py` 导出

---

### Stage 3c — 集成到 Agent

**目标**: bot 调用 `!searchWiki` 时走本地混合检索

任务清单：
- [x] `executor.py`：`set_wiki_store(store)` + `_search_wiki` 改造
- [x] `core.py`：`Agent.start()` 初始化 `WikiVectorStore` 并注入 executor
- [x] 日志记录结果来源（`local` / `http`）及检索耗时
- [ ] LRU 缓存最近 50 条查询，避免同 turn 重复嵌入

---

### Stage 3d — 测试

任务清单：
- [x] `tests/test_wiki_store.py`：mock BGEM3FlagModel + FlagReranker + FAISS，验证 search/load/fallback
- [ ] `tests/test_ingest_wiki.py`：mock MediaWiki API，验证抓取/解析/分块/编码流程
- [ ] 集成冒烟测试：用 5 页真实 wiki 建小索引，验证端到端中英文检索

---

### Stage 3e（可选后续）— 质量与性能提升

| 项目 | 说明 | 收益 |
|---|---|---|
| ColBERT 多向量检索 | BGE-M3 `return_colbert_vecs=True` + MaxSim 计算 | 更高精度，存储 10× |
| 增量更新 | 按页面修改时间戳增量抓取，追加向量而非重建 | 保持索引新鲜 |
| GPU offload | 仅重排时切换 device，编码后释放 | 降低 VRAM 占用 |
| 中文 wiki 镜像 | `zh.minecraft.wiki` 中文页面建独立索引 | 中文查询零翻译 |
| 查询扩展 | 用 LLM 将中文查询扩展为英文关键词后再检索 | 跨语言效果 +10% |

---

## 关键设计决策

### 为什么 BGE-M3 sparse 而非独立 BM25？

传统 BM25（rank-bm25）是词频统计，不理解语义，且需要维护独立分词+索引。BGE-M3 的 lexical_weights 是模型学习到的词权重，语义更准确（对 Minecraft item 名称如 `oak_planks` 尤其有效），且与 dense 向量来自同一模型，维护成本更低。

### 为什么 RRF 而非加权平均？

加权平均需要调参（dense 权重 α vs sparse 权重 1-α），且两路得分的量纲不同（cosine vs dot product），直接相加不合理。RRF 只依赖排名不依赖分数绝对值，无需调参，对两路分数量纲不敏感，是混合检索的工业标准。

### 为什么 top-20 进重排而非 top-50？

重排器延迟与候选数量线性相关。top-20 在 CPU 上约 500ms，top-50 约 1200ms。实验表明 RRF 后正确答案几乎都在 top-20 内，top-20 → top-50 的召回提升 < 2%，不值得付出 2× 延迟。

### 为什么选 `IndexFlatIP` 而非 `IndexIVFFlat`？

50k–500k 块规模下 `IndexFlatIP` 单次检索 < 5ms（CPU），无需量化。超过 100 万块时再考虑 IVF，届时只需重建索引，`WikiVectorStore` 接口不变。

### 为什么保留 HTTP 兜底？

索引未建立时 bot 不应完全失去 wiki 能力。HTTP 兜底保证零配置即可运行，渐进迁移。兜底路径的噪音问题由 F1/F3 loop guard 兜底。

### 分块大小选 400 词的理由

- 太小（< 100 词）：LLM 从单块无法得出完整信息
- 太大（> 600 词）：不相关内容稀释相似度，重排器输入过长
- 400 词 ≈ 2–3 段落，与 Minecraft wiki 分节粒度匹配
- 重排器 `bge-reranker-v2-m3` 上下文窗口 8192 token，400 词约 550 token，远未超限

---

## 运行方式

```bash
# 安装依赖
pip install FlagEmbedding faiss-cpu scipy mwparserfromhell

# 小规模测试（~50 页，首次含模型下载约 10–15 分钟）
python scripts/ingest_wiki.py --max-pages 50 --categories Crafting,Items

# 完整建索引（~1000 页，约 30–60 分钟，取决于硬件）
python scripts/ingest_wiki.py

# 查看结果
cat data/wiki/manifest.json
# {"embed_model": "BAAI/bge-m3", "dim": 1024, "num_chunks": 8432, ...}
```

建索引完成后，重启 bot 即自动加载本地混合索引，`searchWiki` 不再需要联网。
中文命令（"床的配方"、"怎么打苦力怕"）与英文命令同等检索质量。
