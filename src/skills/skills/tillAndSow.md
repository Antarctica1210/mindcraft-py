---
name: api.tillAndSow
description: Till soil and plant seeds to set up a crop farm
keywords: till, sow, plant, farm, seeds, wheat, farmland, grow, crop, 种地, 农场, 播种
---

Calls the built-in `api.tillAndSow(x, y, z, seedType)` helper. It tills one dirt/grass block at the given coordinates and optionally plants seeds on the resulting farmland.

Parameters:
- `x` (number): X coordinate of the dirt/grass block.
- `y` (number): Y coordinate of the dirt/grass block.
- `z` (number): Z coordinate of the dirt/grass block.
- `seedType` (string, optional): Seed item to plant. Default `"wheat_seeds"`. Also accepts `"carrot"`, `"potato"`, `"beetroot_seeds"`, `"melon_seeds"`, `"pumpkin_seeds"`.

Returns a status string.

The API requires a hoe and seed item to already be in inventory.

Example:
```js
const state = await api.observe();
const p = state.position;
await api.tillAndSow(Math.floor(p.x), Math.floor(p.y - 1), Math.floor(p.z), "wheat_seeds");

await api.tillAndSow(100, 63, -50, "carrot");
```
