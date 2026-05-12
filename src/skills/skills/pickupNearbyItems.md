---
name: skills.pickupNearbyItems
description: Pick up all nearby dropped items on the ground
keywords: pickup, collect, items, gather, ground, drops
---

Pick up all items within 8 blocks.

Returns: `true` if items were picked up.

Navigates to each item and collects it.

Example:
```js
await skills.pickupNearbyItems(bot);
```
