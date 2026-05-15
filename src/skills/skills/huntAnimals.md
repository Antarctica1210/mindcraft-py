---
name: skills.huntAnimals
description: Search for, chase, and kill a passive animal for food drops
keywords: hunt, kill, food, meat, animal, cow, sheep, pig, chicken, mutton, beef, pork, 猎, 打猎, 食物, 肉
---

Searches for a passive food animal within range, navigates to it, and kills it to collect meat and other drops. Used when the bot or player needs food and no cooked food is available.

Parameters:
- `animalType` (string, optional): The animal to hunt. Default `"cow"`. Accepts `"cow"`, `"pig"`, `"sheep"`, `"chicken"`, `"rabbit"`.
- `range` (number, optional): Search radius in blocks. Default `64`.

Returns: `true` if an animal was killed and drops collected, `false` if none found.

Food drop reference:

| Animal | Raw drop | Cooked drop (if on fire) |
|---|---|---|
| cow | raw_beef | cooked_beef |
| pig | raw_porkchop | cooked_porkchop |
| sheep | raw_mutton + wool | cooked_mutton |
| chicken | raw_chicken | cooked_chicken |
| rabbit | raw_rabbit | cooked_rabbit |

The skill:
- Equips the best available weapon before attacking
- Picks up all dropped items after the kill
- If no animal found within `range`, returns `false` — caller should try a wider radius or a different animal type

Example:
```js
// Hunt the nearest cow within 64 blocks
await skills.huntAnimals(bot, "cow", 64);

// Fall back to chicken if preferred animal not found
const got = await skills.huntAnimals(bot, "cow", 64);
if (!got) await skills.huntAnimals(bot, "chicken", 128);
```
