#!/usr/bin/env node
/**
 * Phase 7a — Export minecraft-data JSON for Python KnowledgeGraph.
 *
 * Reads the minecraft-data npm package for a given Minecraft version and
 * writes structured JSON files to ``data/minecraft/<version>/``.
 *
 * Usage:
 *   node scripts/export_minecraft_data.js [version] [--output-dir <path>]
 *
 * Default version: 1.21.6
 * Default output:  data/minecraft/<version>/
 *
 * Exported files:
 *   - items.json      (array of item objects: id, name, displayName, stackSize)
 *   - blocks.json     (array of block objects: id, name, displayName, hardness,
 *                       harvestTools, material, boundingBox)
 *   - recipes.json    (array of recipe objects: id, result, ingredients,
 *                       requires_table, shape)
 *   - entities.json   (array of entity objects: id, name, type, width, height)
 *   - foods.json      (array of food objects: id, name, foodPoints, saturation)
 */

const fs = require("fs");
const path = require("path");

// Parse args
const args = process.argv.slice(2);
let version = "1.21.6";
let outputDir = null;

for (let i = 0; i < args.length; i++) {
  if (args[i] === "--output-dir" && args[i + 1]) {
    outputDir = args[i + 1];
    i++;
  } else if (!args[i].startsWith("--")) {
    version = args[i];
  }
}

const minecraftData = require("minecraft-data");
const mcData = minecraftData(version);

if (!mcData) {
  console.error(`Failed to load minecraft-data for version ${version}`);
  process.exit(1);
}

console.log(
  `Exporting minecraft-data ${mcData.version.majorVersion} (dataVersion: ${mcData.version.dataVersion})`
);

const outRoot = outputDir || path.join("data", "minecraft", version);
if (!fs.existsSync(outRoot)) {
  fs.mkdirSync(outRoot, { recursive: true });
}

// ── Helpers ───────────────────────────────────────────────────────────

function toArray(obj) {
  /** Convert {id: {...}} object to sorted array of values. */
  if (Array.isArray(obj)) return obj;
  if (obj && typeof obj === "object") {
    return Object.values(obj).sort((a, b) => (a.id || 0) - (b.id || 0));
  }
  return [];
}

function writeJson(filename, data) {
  const filepath = path.join(outRoot, filename);
  fs.writeFileSync(filepath, JSON.stringify(data, null, 2));
  console.log(`  Wrote ${data.length} entries → ${filepath}`);
}

// ── Export each dataset ────────────────────────────────────────────────

// Items: id, name, displayName, stackSize
const items = toArray(mcData.items).map((item) => ({
  id: item.id,
  name: item.name,
  displayName: item.displayName || "",
  stackSize: item.stackSize || 64,
}));
writeJson("items.json", items);

// Blocks: id, name, displayName, hardness, harvestTools, boundingBox
const blocks = toArray(mcData.blocks).map((block) => ({
  id: block.id,
  name: block.name,
  displayName: block.displayName || "",
  hardness: block.hardness || 0,
  harvestTools: block.harvestTools || {},
  boundingBox: block.boundingBox || "empty",
  material: block.material || "",
}));
writeJson("blocks.json", blocks);

// Build ID → name lookup from items
const itemIdToName = {};
for (const item of items) {
  itemIdToName[item.id] = item.name;
}

// Recipes: the recipes object maps result ID → array of recipe variants.
// Each recipe has inShape (2D item ID grid) or ingredients (flat item ID list).
function resolveIngredientNames(recipe) {
  /** Return sorted unique item names used in this recipe. */
  const ids = new Set();
  if (recipe.inShape) {
    for (const row of recipe.inShape) {
      for (const id of row) {
        if (id !== 0 && id !== null && id !== undefined) ids.add(id);
      }
    }
  }
  if (recipe.ingredients && Array.isArray(recipe.ingredients)) {
    for (const id of recipe.ingredients) {
      if (id !== 0 && id !== null && id !== undefined) ids.add(id);
    }
  }
  if (recipe.ingredients && !Array.isArray(recipe.ingredients)) {
    for (const v of Object.values(recipe.ingredients)) {
      if (Array.isArray(v)) {
        for (const sub of v) {
          if (sub.id && sub.id !== 0) ids.add(sub.id);
        }
      } else if (v && v.id && v.id !== 0) {
        ids.add(v.id);
      }
    }
  }
  return [...ids].map((id) => itemIdToName[id] || `unknown_${id}`).sort();
}

function translateShape(inShape) {
  if (!inShape) return null;
  return inShape.map((row) =>
    row.map((id) => (id === 0 ? null : itemIdToName[id] || `unknown_${id}`))
  );
}

// Build flat recipe list from the result-ID-keyed object
const recipes = [];
for (const [resultId, variants] of Object.entries(mcData.recipes)) {
  const rid = parseInt(resultId, 10);
  if (rid === 0 || !variants || !Array.isArray(variants)) continue;
  for (const variant of variants) {
    const ingredientNames = resolveIngredientNames(variant);
    if (ingredientNames.length === 0) continue;
    recipes.push({
      result: {
        id: rid,
        name: itemIdToName[rid] || `unknown_${rid}`,
        count: (variant.result && variant.result.count) || 1,
      },
      inShape: translateShape(variant.inShape),
      ingredient_names: ingredientNames,
      requires_table: variant.requiresTable || variant.requires_table || false,
    });
  }
}
console.log(`  Built ${recipes.length} recipes from result-ID index`);
writeJson("recipes.json", recipes);

// Entities: id, name, type, width, height, category
const entities = toArray(mcData.entities).map((entity) => ({
  id: entity.id,
  name: entity.name,
  internalName: entity.internalName || "",
  type: entity.type || "",
  category: entity.category || "",
  width: entity.width || 0,
  height: entity.height || 0,
}));
writeJson("entities.json", entities);

// Foods: id, name, foodPoints, saturation
const foods = toArray(mcData.foods).map((food) => ({
  id: food.id,
  name: food.name,
  displayName: food.displayName || "",
  foodPoints: food.foodPoints || 0,
  saturation: food.saturation || 0,
}));
writeJson("foods.json", foods);

console.log(`\nExport complete → ${path.resolve(outRoot)}`);
console.log(
  `  ${items.length} items, ${blocks.length} blocks, ${recipes.length} recipes, ` +
    `${entities.length} entities, ${foods.length} foods`
);
