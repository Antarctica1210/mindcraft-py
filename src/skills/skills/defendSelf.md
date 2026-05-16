---
name: api.attack
description: Attack all nearby hostile mobs to protect the bot
keywords: defend, self defence, hostile, mob, attack, protect, danger, zombie, skeleton, creeper, spider, 防御, 自卫, 保护
---

Use `api.entities()` or `api.observe()` to identify nearby hostile mobs, then call `api.attack(entityType, range)` for the nearest threat. Repeat for each hostile type if needed.

Parameters:
- `entityType` (string): Hostile type to attack, e.g. `"zombie"` or `"skeleton"`.
- `range` (number, optional): Search radius in blocks.

Returns a status string from the attack command.

Common hostile types: `"zombie"`, `"skeleton"`, `"creeper"`, `"spider"`, `"witch"`, `"enderman"`, `"pillager"`.

Example:
```js
await api.attack("zombie", 16);
await api.attack("skeleton", 16);
```
