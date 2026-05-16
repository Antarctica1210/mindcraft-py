---
name: api.equip
description: Equip an item to the proper body slot
keywords: equip, wear, hold, armor, tool, weapon
---

Calls the built-in `api.equip(itemName, destination)` helper.

Parameters:
- `itemName` (string): The item to equip (e.g. "iron_pickaxe", "diamond_chestplate")

Returns: `true` if equipped, `false` if item not found.

Automatically determines the correct slot:
- Leggings → legs, Boots → feet, Helmet → head
- Chestplate/Elytra → torso, Shield → off-hand
- Other → main hand

Example:
```js
await api.equip("iron_sword", "hand");
```
