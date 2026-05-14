# Minecraft Data Sources

This directory is populated from the vendored PrismarineJS `minecraft-data`
package under `node_modules/minecraft-data/minecraft-data/data/pc`.

- `items.json`, `blocks.json`, `entities.json`, `foods.json`, `recipes.json`: existing 1.21.6 exports.
- `biomes.json`, `materials.json`, `version.json`: copied from `pc/1.21.6`.
- `features.json`: copied from `pc/common/features.json`.
- `blockLoot.json`, `entityLoot.json`: copied from `pc/1.20` as the nearest
  available Java loot baseline in the vendored upstream data. Treat these as
  compatibility data, not exact 1.21.6 loot-table extracts.
