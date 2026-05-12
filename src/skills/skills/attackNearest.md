---
name: skills.attackNearest
description: Attack the nearest hostile mob of the given type
keywords: attack, fight, kill, combat, mob, hostile, defend
---

Attack a mob of the given type. Optionally continues until the mob is dead.

Parameters:
- `mobType` (string): The type of mob to attack (e.g. "zombie")
- `kill` (boolean, optional): Continue attacking until dead, default true

Returns: `true` if the mob was attacked, `false` if not found.

Equips the highest-damage weapon before attacking.

Example:
```js
await skills.attackNearest(bot, "zombie", true);
```
