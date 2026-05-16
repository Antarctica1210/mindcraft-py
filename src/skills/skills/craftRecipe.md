---
name: api.craftRecipe
description: Craft an item from a recipe using available materials
keywords: craft, recipe, crafting, make, create
---

Calls the built-in `api.craftRecipe(itemName, count)` helper. Attempts to craft the given item name from a recipe.

Parameters:
- `itemName` (string): The item name to craft
- `num` (number, optional): How many to craft, default 1

Returns: `true` if the recipe was crafted, `false` otherwise.

The skill automatically:
- Checks if a crafting table is required and places one if needed
- Calculates the limiting resource and crafts as many as possible
- Equips any crafted armor automatically
- Picks up the crafting table afterward if one was placed

Example:
```js
await api.craftRecipe("stick", 4);
```
