---
name: skills.moveAway
description: Move the bot away from its current position by a given distance
keywords: move away, retreat, back off, flee, escape, run, distance, danger, 撤退, 逃跑, 远离, 后退
---

Moves the bot in a direction away from its current position. Use this to create distance from a hazard (hostile mob, lava, explosion), to clear space before placing blocks, or to back away after a failed action.

Parameters:
- `distance` (number, optional): How many blocks to move away. Default `16`.

Returns: `true` if the bot successfully moved away, `false` if pathfinding failed.

The skill:
- Picks a random safe direction away from the current position
- Uses pathfinder to navigate — will not walk into lava or off cliffs
- Does not target a specific destination, only moves `distance` blocks in the chosen direction
- Combines well with `defendSelf` (retreat → heal → re-engage) or with `searchForEntity` (move away from a mob before re-scanning)

Example:
```js
// Back off 16 blocks (default)
await skills.moveAway(bot);

// Back off further when in serious danger
await skills.moveAway(bot, 32);

// Pattern: retreat, eat, then reassess
await skills.moveAway(bot, 20);
await skills.consume(bot, "cooked_beef");
```
