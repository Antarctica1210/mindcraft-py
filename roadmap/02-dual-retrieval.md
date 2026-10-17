# P1: 双层检索 + 持久化向量索引

## 动机

当前 `Examples` 和 `SkillLibrary` 的检索仅使用稠密向量余弦相似度，且无索引结构（全量遍历）。降级方案是 Jaccard 词重叠，两者各自有明显盲区：

- 稠密向量：语义匹配好，但可能漏掉精确关键词匹配
- Jaccard 词重叠：精确匹配好，但完全无视语义相似性

## 当前实现

```javascript
// src/utils/examples.js:46-63
let embedding = await this.model.embed(turn_text);
this.examples.sort((a, b) => 
    cosineSimilarity(embedding, this.embeddings[this.turnsToText(b)]) -
    cosineSimilarity(embedding, this.embeddings[this.turnsToText(a)])
);

// 降级方案
this.examples.sort((a, b) => 
    wordOverlapScore(turn_text, this.turnsToText(b)) -
    wordOverlapScore(turn_text, this.turnsToText(a))
);
```

## 设计

```
查询 "how to build a stone house"

     ┌─────────── 稠密检索 (embedding) ──────────┐
     │  语义匹配 "build shelter with cobblestone" │
     │  使用现有 embedding model                   │
     └────────────────────────────────────────────┘
                          +
     ┌─────────── 稀疏检索 (BM25) ────────────────┐
     │  关键词匹配 "stone", "house", "build"       │
     │  可用 lunr.js 或 custom BM25               │
     └────────────────────────────────────────────┘
                          ↓
     ┌─────────── 融合 (RRF) ─────────────────────┐
     │  score = 1/(k+dense_rank) + 1/(k+sparse_rank)│
     │  k 通常取 60                                │
     └────────────────────────────────────────────┘
                          ↓
     ┌─────────── 重排序 (Cross-encoder) ─────────┐
     │  精确打分，取最终 top-N                       │
     │  可选：小模型本地推理 或 额外 LLM 调用        │
     └────────────────────────────────────────────┘
```

## BM25 实现方案

```javascript
// 轻量级，无需外部依赖
class BM25Index {
  constructor(documents) {
    this.documents = documents;
    this.avgdl = documents.reduce((s, d) => s + d.length, 0) / documents.length;
    this.idf = this._computeIDF(documents);
  }

  search(query, k1 = 1.5, b = 0.75) {
    const queryTerms = this._tokenize(query);
    return this.documents.map((doc, idx) => ({
      idx,
      score: queryTerms.reduce((s, term) =>
        s + this._bm25Score(term, doc, idx, k1, b), 0
      )
    })).sort((a, b) => b.score - a.score);
  }
}
```

## 向量持久化方案

当前 embedding 在每次进程启动时重新计算。可持久化到文件：

```javascript
// 存储
const index = {
  version: 1,
  model: "gemini-embedding-001",
  entries: [
    { text: "...", embedding: [0.1, 0.2, ...] },
    { text: "...", embedding: [0.3, 0.4, ...] }
  ]
};
fs.writeFileSync('./bots/andy/embedding_cache.json', JSON.stringify(index));
```

启动时先检查缓存，仅对新文档计算 embedding。

## 关键模块改动

| 模块 | 改动 |
|------|------|
| `utils/examples.js` | 新增 BM25 索引，RRF 融合逻辑 |
| `agent/library/skill_library.js` | 同上 |
| 新文件 `utils/bm25.js` | BM25 实现 |
| 新文件 `utils/retrieval.js` | 统一检索接口（稠密+稀疏+重排序） |

## 收益

- 精确关键词匹配不会被稠密向量「平滑掉」
- 余弦相似度结果中的误匹配可被 BM25 校正
- 向量缓存减少启动时的 API 调用成本
