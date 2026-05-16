---
name: api.placeBlock
description: Place a block at specified coordinates
keywords: place, build, put, block, construct
---

Calls the built-in `api.placeBlock(blockType)` helper. Place the given block type at the bot's current position.

Parameters:
- `blockType` (string): The block or item name to place
- `blockType` (string): The block or item name to place.

Returns: `true` if the block was placed, `false` otherwise.

Move to the target location first with `api.moveTo`, then call `api.placeBlock`.

Example:
```js
await api.moveTo(100, 64, -50);
await api.placeBlock("torch");
```
