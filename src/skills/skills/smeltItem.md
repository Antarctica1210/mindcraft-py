---
name: skills.smeltItem
description: Smelt items in a furnace
keywords: smelt, furnace, cook, heat, iron, gold, food
---

Puts fuel in a furnace and smelts the given item. Waits until done.

Parameters:
- `itemName` (string): The item to smelt (e.g. "raw_iron", "beef")
- `num` (number, optional): Number of items to smelt, default 1

Returns: `true` if smelted successfully, `false` otherwise.

The skill:
- Finds or places a furnace nearby
- Adds coal/charcoal/wood as fuel if needed
- Waits for smelting to complete
- Clears the furnace and picks it up if it was placed

Example:
```js
await skills.smeltItem(bot, "raw_iron", 3);
```
