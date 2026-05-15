---
name: skills.tillAndSow
description: Till soil and plant seeds to set up a crop farm
keywords: till, sow, plant, farm, seeds, wheat, farmland, grow, crop, 种地, 农场, 播种
---

Tills the dirt or grass around the bot's current position and plants seeds on the resulting farmland. Must be near a water source (within 4 blocks) for crops to grow.

Parameters:
- `pos` (Vec3, optional): Centre position to till around. Defaults to bot's current position.
- `width` (number, optional): Width of the plot in blocks. Default `3`.
- `length` (number, optional): Length of the plot in blocks. Default `3`.
- `seedType` (string, optional): Seed item to plant. Default `"wheat_seeds"`. Also accepts `"carrot"`, `"potato"`, `"beetroot_seeds"`, `"melon_seeds"`, `"pumpkin_seeds"`.

Returns: number of seeds planted, or `0` if no farmland could be tilled.

The skill:
- Equips a hoe automatically (crafts one if none available and wood is in inventory)
- Tills every tillable block in the plot
- Plants seeds on each farmland block if the seed item is in inventory
- Skips blocks already tilled or already planted

Example:
```js
// Till a 3×3 plot and plant wheat
await skills.tillAndSow(bot, bot.entity.position, 3, 3, "wheat_seeds");

// Plant carrots on an existing 5×5 farm
await skills.tillAndSow(bot, bot.entity.position, 5, 5, "carrot");
```