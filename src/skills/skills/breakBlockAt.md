---
name: skills.breakBlockAt
description: Break the block at specific coordinates
keywords: break, destroy, mine, dig, remove
---

Break the block at the given position. Uses the bot's equipped item.

Parameters:
- `x` (number): X coordinate
- `y` (number): Y coordinate
- `z` (number): Z coordinate

Returns: `true` if the block was broken, `false` otherwise.

Equips the correct tool for the block type before breaking.

Example:
```js
let p = world.getPosition(bot);
await skills.breakBlockAt(bot, p.x, p.y - 1, p.z);
```
