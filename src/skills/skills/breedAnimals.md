---
name: skills.breedAnimals
description: Feed two animals of the same type to trigger breeding
keywords: breed, breeding, baby, animal, cow, sheep, pig, chicken, reproduce, farm, 繁殖, 养殖, 配种
---

Finds two nearby animals of the given type and feeds each one the appropriate food item to put them into love mode. A baby animal spawns shortly after.

Parameters:
- `animalType` (string): The animal to breed (e.g. `"cow"`, `"sheep"`, `"pig"`, `"chicken"`, `"rabbit"`).
- `breedingItem` (string, optional): Food item to feed them. Auto-detected from `animalType` if omitted.

Returns: `true` if both animals were fed and entered love mode, `false` otherwise.

Breeding food map (used when `breedingItem` is omitted):

| Animal | Food |
|---|---|
| cow, mooshroom | wheat |
| sheep | wheat |
| pig | carrot, potato, or beetroot |
| chicken | wheat_seeds, melon_seeds, pumpkin_seeds, or beetroot_seeds |
| rabbit | carrot, golden_carrot, or dandelion |
| horse, donkey | golden_apple or golden_carrot |
| llama | hay_block |

The skill:
- Searches within 32 blocks for two adults of the given type
- Navigates close to each animal before feeding
- Requires the food item to already be in inventory
- Animals must not be in cooldown (20-minute cooldown after previous breeding)

Example:
```js
// Breed cows (auto-detects wheat)
await skills.breedAnimals(bot, "cow");

// Breed pigs with carrot explicitly
await skills.breedAnimals(bot, "pig", "carrot");

// Breed chickens
await skills.breedAnimals(bot, "chicken", "wheat_seeds");
```