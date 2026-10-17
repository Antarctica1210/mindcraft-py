---
name: api.moveTo
description: Navigate to specific coordinates
keywords: go, move, navigate, position, coordinates, travel
---

Calls the built-in `api.moveTo(x, y, z)` helper.

Parameters:
- `x` (number): X coordinate
- `y` (number): Y coordinate
- `z` (number): Z coordinate
- `min_distance` (number, optional): How close to get, default 2

Returns: `true` if reached, `false` otherwise.

Uses door-opening helpers and will find a path around obstacles.

Example:
```js
await api.moveTo(100, 64, -50);
```
