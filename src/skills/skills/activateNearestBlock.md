---
name: api.activateBlock
description: Right-click / interact with the nearest block of a given type
keywords: activate, use, interact, open, right-click, bed, furnace, chest, door, lever, button, crafting table, 使用, 交互, 激活, 打开
---

Calls the built-in `api.activateBlock(blockType)` helper. It navigates to and right-clicks (activates) the nearest block of the given type. Use this to sleep in a bed, open a furnace or chest GUI, press a button, flip a lever, open a door, or interact with any other activatable block.

Parameters:
- `blockType` (string): The block to activate (e.g. `"bed"`, `"furnace"`, `"chest"`, `"crafting_table"`, `"lever"`, `"oak_door"`).

Returns: `true` if the block was found and activated, `false` if not found within 32 blocks.

The skill:
- Searches within 32 blocks for the nearest matching block
- Pathfinds to within interaction range (≤ 4 blocks)
- Sends a right-click activation to the block
- Does not manage the resulting GUI (e.g. does not insert items into a chest)

Common use cases:

| Goal | Call |
|---|---|
| Sleep through the night | `api.activateBlock("bed")` |
| Open furnace to check output | `api.activateBlock("furnace")` |
| Open a chest | `api.activateBlock("chest")` |
| Use crafting table | `api.activateBlock("crafting_table")` |
| Flip a lever | `api.activateBlock("lever")` |

Example:
```js
// Sleep in the nearest bed
await api.activateBlock("bed");

// Interact with the nearest furnace
await api.activateBlock("furnace");

// Open a chest
await api.activateBlock("chest");
```
