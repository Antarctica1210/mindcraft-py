---
name: skills.defendSelf
description: Attack all nearby hostile mobs to protect the bot
keywords: defend, self defence, hostile, mob, attack, protect, danger, zombie, skeleton, creeper, spider, 防御, 自卫, 保护
---

Scans for hostile mobs within range and attacks each one until killed. Continues until no hostiles remain or the timeout expires. Use this reactively when the bot is under attack or when `!entities` reveals nearby threats.

Parameters:
- `range` (number, optional): Detection radius in blocks. Default `8`.
- `timeout` (number, optional): Maximum milliseconds to spend defending. Default `10000` (10 seconds).

Returns: `true` if all detected hostiles were eliminated, `false` if timed out or bot health dropped critically.

The skill:
- Equips the highest-damage melee weapon in inventory before engaging
- Prioritises the closest hostile mob first
- Retreats 3 blocks if health drops below 4 (2 hearts) and eats food if available
- Re-scans after each kill in case new mobs arrived

Common hostile types: `"zombie"`, `"skeleton"`, `"creeper"`, `"spider"`, `"witch"`, `"enderman"`, `"pillager"`.

Example:
```js
// Defend against anything within 8 blocks (default)
await skills.defendSelf(bot);

// Wider sweep, longer timeout for a dungeon
await skills.defendSelf(bot, 16, 30000);
```
