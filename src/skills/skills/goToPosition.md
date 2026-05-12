---
name: skills.goToPosition
description: Navigate to specific coordinates
keywords: go, move, navigate, position, coordinates, travel
---

Navigate to the given position using pathfinding.

Parameters:
- `x` (number): X coordinate
- `y` (number): Y coordinate
- `z` (number): Z coordinate
- `min_distance` (number, optional): How close to get, default 2

Returns: `true` if reached, `false` otherwise.

Uses door-opening helpers and will find a path around obstacles.

Example:
```js
await skills.goToPosition(bot, 100, 64, -50, 2);
```
