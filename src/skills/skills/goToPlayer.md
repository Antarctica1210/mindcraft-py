---
name: api.goToPlayer
description: Navigate to a specific player
keywords: go, navigate, move, player, follow, travel
---

Calls the built-in `api.goToPlayer(username, closeness)` helper.

Parameters:
- `username` (string): The player to navigate to
- `distance` (number, optional): Goal distance to the player, default 3

Returns: `true` if the player was reached, `false` otherwise.

Uses pathfinding and will open doors along the way.

Example:
```js
await api.goToPlayer("Steve", 2);
```
