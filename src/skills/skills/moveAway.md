---
name: api.moveAway
description: Move the bot away from its current position by a given distance
keywords: move away, retreat, back off, flee, escape, run, distance, danger, 撤退, 逃跑, 远离, 后退
---

Calls the built-in `api.moveAway(distance)` helper. Use this to create distance from a hazard, clear space before placing blocks, or back away after a failed action.

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
await api.moveAway();

// Back off further when in serious danger
await api.moveAway(32);

// Pattern: retreat, eat, then reassess
await api.moveAway(20);
await api.consume("cooked_beef");
```
