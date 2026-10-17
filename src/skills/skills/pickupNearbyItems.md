---
name: api.pickupNearbyItems
description: Pick up all nearby dropped items on the ground
keywords: pickup, collect, items, gather, ground, drops
---

Calls the built-in `api.pickupNearbyItems(distance)` helper. Pick up all items within range.

Returns: `true` if items were picked up.

Navigates to each item and collects it.

Example:
```js
await api.pickupNearbyItems(8);
```
