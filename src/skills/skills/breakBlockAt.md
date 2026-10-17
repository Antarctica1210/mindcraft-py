---
name: api.breakNearbyBlocks
description: Break nearby reachable blocks of a given type
keywords: break, destroy, mine, dig, remove
---

Calls the built-in `api.breakNearbyBlocks(blockType, count, range)` helper. Use this for nearby fragile blocks such as tall grass, flowers, or other blocks already within reach.

Parameters:
- `blockType` (string): Block type to break, e.g. `"tall_grass"`
- `count` (number, optional): Number to break, default 1
- `range` (number, optional): Local search radius, default 6

Returns a status string with broken count and inventory delta.

Prefer `api.collectBlocks` for mining durable blocks that need pathfinding.

Example:
```js
await api.breakNearbyBlocks("tall_grass", 3, 6);
```
