---
name: api.attack
description: Search for, chase, and kill a passive animal for food drops
keywords: hunt, kill, food, meat, animal, cow, sheep, pig, chicken, mutton, beef, pork, 猎, 打猎, 食物, 肉
---

Use `api.searchForEntity`, `api.attack`, and `api.pickupNearbyItems` to hunt a passive food animal and collect drops. Used when the bot or player needs food and no cooked food is available.

Parameters:
- `animalType` (string, optional): The animal to hunt. Default `"cow"`. Accepts `"cow"`, `"pig"`, `"sheep"`, `"chicken"`, `"rabbit"`.
- `range` (number, optional): Search radius in blocks. Default `64`.

Returns status strings from the built-in helpers.

Food drop reference:

| Animal | Raw drop | Cooked drop (if on fire) |
|---|---|---|
| cow | raw_beef | cooked_beef |
| pig | raw_porkchop | cooked_porkchop |
| sheep | raw_mutton + wool | cooked_mutton |
| chicken | raw_chicken | cooked_chicken |
| rabbit | raw_rabbit | cooked_rabbit |

If no animal is found within `range`, try a wider radius or a different animal type.

Example:
```js
// Hunt the nearest cow within 64 blocks
await api.searchForEntity("cow", 64);
await api.attack("cow", 64);
await api.pickupNearbyItems(8);

// Fall back to chicken if preferred animal not found
await api.searchForEntity("chicken", 64);
await api.attack("chicken", 64);
await api.pickupNearbyItems(8);
```
