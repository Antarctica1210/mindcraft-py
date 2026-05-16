---
name: api.attack
description: Attack the nearest hostile mob of the given type
keywords: attack, fight, kill, combat, mob, hostile, defend
---

Calls the built-in `api.attack(entityType, range)` helper. Attack the nearest mob/entity of the given type.

Parameters:
- `entityType` (string): The type of mob/entity to attack (e.g. "zombie", "cow")
- `range` (number, optional): Search radius, default 16

Returns: `true` if the mob was attacked, `false` if not found.

The socket command handles approaching and attacking the target.

Example:
```js
await api.attack("zombie", 16);
```
