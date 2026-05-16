可以，脚本已经支持 `--delay`。你现在这批知识建议先建一个“生存工作流”索引，不要一次抓全 wiki。

用这个命令：

```bash
uv run python scripts/ingest_wiki.py \
  --output data/wiki-survival \
  --categories "Crafting,Blocks,Items,Mobs,Food,Mechanics" \
  --max-pages 500 \
  --delay 1.2 \
  --chunk-size 420 \
  --chunk-overlap 70 \
  --batch-size 8 \
  --embed-model models/BAAI/bge-m3 \
  --device mps \
  --fp16
```

这个会覆盖你要的四类：
- 制作：`Crafting` + 固定页里的 `Crafting / Smelting / Furnace / Crafting table`
- 挖矿：`Blocks / Items / Mechanics` + 固定页里的 `Mining / Pickaxe`
- 种植：固定页里的 `Farming / Hoe`，再从 `Food / Items / Blocks` 补种子、作物、方块
- 养动物：`Mobs / Food / Items` 覆盖动物、繁殖食物、掉落物

`--delay 1.2` 表示每抓一个页面等 1.2 秒，比较温和。想更保守可以改成 `--delay 2.0`。

如果下载中断了，后面可以不重新请求服务器，直接用已经抓下来的 raw 文件重建索引：

```bash
uv run python scripts/ingest_wiki.py \
  --output data/wiki-survival \
  --from-raw \
  --batch-size 8 \
  --embed-model models/BAAI/bge-m3 \
  --device mps \
  --fp16
```

建好后 `.env` 指向：

```env
WIKI_RAG_ENABLED=true
WIKI_RAG_INDEX_DIR=data/wiki-survival
WIKI_RAG_EMBED_MODEL=models/BAAI/bge-m3
WIKI_RAG_RERANK_MODEL=models/BAAI/bge-reranker-v2-m3
WIKI_RAG_DEVICE=mps
WIKI_RAG_USE_FP16=true
```