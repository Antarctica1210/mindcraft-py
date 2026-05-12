---
name: skills.goToPlayer
description: Navigate to a specific player
keywords: go, navigate, move, player, follow, travel
---

Navigate to the given player.

Parameters:
- `username` (string): The player to navigate to
- `distance` (number, optional): Goal distance to the player, default 3

Returns: `true` if the player was reached, `false` otherwise.

Uses pathfinding and will open doors along the way.

Example:
```js
await skills.goToPlayer(bot, "Steve", 2);
```
