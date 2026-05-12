---
name: skills.placeBlock
description: Place a block at specified coordinates
keywords: place, build, put, block, construct
---

Place the given block type at the given position. Builds off from any adjacent blocks.

Parameters:
- `blockType` (string): The block or item name to place
- `x` (number): X coordinate
- `y` (number): Y coordinate
- `z` (number): Z coordinate
- `placeOn` (string, optional): Preferred side — "top", "bottom", "north", "south", "east", "west", "side". Default "bottom".
- `dontCheat` (boolean, optional): Override cheat mode. Default false.

Returns: `true` if the block was placed, `false` otherwise.

Will fail if there's a block in the way or nothing to build off of.

Example:
```js
let p = world.getPosition(bot);
await skills.placeBlock(bot, "torch", p.x + 1, p.y, p.z, "side");
```
