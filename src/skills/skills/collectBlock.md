---
name: skills.collectBlock
description: Mine and collect a specified number of blocks
keywords: collect, mine, gather, block, harvest, dig
---

Collect one or more blocks of the given type.

Parameters:
- `blockType` (string): The type of block to collect (e.g. "oak_log")
- `num` (number, optional): Number to collect, default 1
- `exclude` (array, optional): Positions to exclude from search

Returns: `true` if at least one block was collected, `false` otherwise.

The skill handles:
- Ore variants (e.g. "coal" also matches "coal_ore" and "deepslate_coal_ore")
- Tool equipping and harvest capability checks
- Liquid collection via buckets
- Safety checks (won't mine under falling blocks)

Example:
```js
await skills.collectBlock(bot, "oak_log", 8);
```
