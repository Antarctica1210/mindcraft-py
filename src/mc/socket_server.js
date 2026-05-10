/**
 * Mineflayer Socket Server — thin TCP bridge for the Python agent.
 *
 * Listens on a TCP port (default 9090), wraps a single Mineflayer bot,
 * and speaks a JSON-line protocol:
 *
 *   Request  → {"id":"…","method":"…","params":{…}}
 *   Response ← {"id":"…","ok":true|false,"result":…|"error":"…"}
 *   Event    ← {"event":"…","data":{…}}
 *
 * Usage:
 *   node src/mc/socket_server.js [--port 9090]
 *
 * The server auto-quits 5 s after the TCP client disconnects.
 */

import * as net from "node:net";
import { parseArgs } from "node:util";

const { values: opts } = parseArgs({
  options: { port: { type: "string", default: "9090" } },
  strict: false,
});
const PORT = parseInt(opts.port, 10);

// ── Lazy-loaded modules ──────────────────────────────────────────────

let mineflayer, pathfinder, collectBlock, pvp, armorManager, autoEat, vec3;

async function loadModules() {
  if (mineflayer) return;
  mineflayer = (await import("mineflayer")).default;
  pathfinder = (await import("mineflayer-pathfinder")).default;
  collectBlock = (await import("mineflayer-collectblock")).default;
  pvp = (await import("mineflayer-pvp")).default;
  armorManager = (await import("mineflayer-armor-manager")).default;
  autoEat = (await import("mineflayer-auto-eat")).default;
  vec3 = (await import("vec3")).default;
}

function loadPlugins(b) {
  b.loadPlugin(pathfinder.pathfinder);
  b.loadPlugin(collectBlock.plugin);
  b.loadPlugin(pvp.plugin);
  b.loadPlugin(armorManager);
  b.loadPlugin(autoEat);
}

// ── Bot state ────────────────────────────────────────────────────────

let bot = null;
let clientSocket = null;
let lineBuffer = "";
let followingTarget = null;   // player name being followed
let stayingUntil = 0;         // timestamp when stay() ends (0 = not staying)

// ── Push events ──────────────────────────────────────────────────────

function emit(event, data) {
  if (clientSocket && !clientSocket.destroyed) {
    clientSocket.write(JSON.stringify({ event, data }) + "\n");
  }
}

function registerBotEvents(b) {
  b.on("chat", (username, message) => emit("chat", { sender: username, message }));
  b.on("end", (reason) => emit("end", { reason: String(reason || "") }));
  b.on("error", (err) => emit("error", { message: err.message }));
  b.on("kicked", (reason) => emit("kicked", { reason: String(reason || "") }));
  b.on("spawn", () => {
    followingTarget = null;
    stayingUntil = 0;
    emit("spawn", {});
  });
}

// ── Observation helpers ──────────────────────────────────────────────

function timeOfDay(b) {
  try {
    const h = (b.time.timeOfDay || 0) / 1000;
    if (h < 6) return "night"; if (h < 7) return "sunrise";
    if (h < 12) return "day"; if (h < 13) return "noon";
    if (h < 18) return "afternoon"; if (h < 19) return "sunset";
    return "night";
  } catch { return ""; }
}

function nearbyEntities(b) {
  try {
    const parts = [];
    for (const [eid, entity] of Object.entries(b.entities || {})) {
      if (parts.length >= 10) break;
      if (entity.name && entity.name !== "player" && entity.position) {
        const dx = b.entity.position.x - entity.position.x;
        const dy = b.entity.position.y - entity.position.y;
        const dz = b.entity.position.z - entity.position.z;
        const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        parts.push(`  ${entity.name} (${dist.toFixed(0)}m) [id:${eid}]`);
      }
    }
    return parts.join("\n");
  } catch { return ""; }
}

function nearbyPlayers(b) {
  try {
    const parts = [];
    for (const [name, player] of Object.entries(b.players || {})) {
      if (parts.length >= 5) break;
      if (name !== b.username && player.entity) {
        const p = player.entity.position;
        parts.push(`  player:${name} at (${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)})`);
      }
    }
    return parts.join("\n");
  } catch { return ""; }
}

function standingBlock(b) {
  try {
    const blk = b.blockAt(b.entity.position);
    return blk ? `standing on: ${blk.name}` : "";
  } catch { return ""; }
}

function inventoryItems(b) {
  try {
    const items = b.inventory.items();
    if (!items?.length) return [];
    return items.slice(0, 30).map((i) => ({ name: i.name || "unknown", count: i.count || 0 }));
  } catch { return []; }
}

function allEntities(b) {
  try {
    const result = [];
    for (const [, e] of Object.entries(b.entities || {})) {
      if (!e.position) continue;
      result.push({
        id: e.id || 0,
        name: e.name || e.username || "unknown",
        type: e.type || "mob",
        position: { x: e.position.x, y: e.position.y, z: e.position.z },
        health: e.health ?? 0,
      });
    }
    return result;
  } catch { return []; }
}

function nearbyBlockTypes(b, distance = 16) {
  try {
    const types = new Set();
    const pos = b.entity.position;
    for (let dx = -distance; dx <= distance; dx += 4) {
      for (let dy = -4; dy <= 4; dy += 2) {
        for (let dz = -distance; dz <= distance; dz += 4) {
          const blk = b.blockAt(pos.offset(dx, dy, dz));
          if (blk && blk.name !== "air") types.add(blk.name);
        }
      }
    }
    return [...types].slice(0, 50);
  } catch { return []; }
}

// ── Navigation helpers ────────────────────────────────────────────────

function ensureMovements(b) {
  if (!b.pathfinder.Movements) {
    b.pathfinder.setMovements(new pathfinder.Movements(b));
  }
}

function findPlayer(b, username) {
  const player = b.players[username];
  if (player?.entity) return player.entity;
  return null;
}

// ── Request handler ──────────────────────────────────────────────────

async function handleRequest(method, params) {
  // ── Lifecycle ────────────────────────────────────────────────────
  switch (method) {
    case "create_bot": {
      if (bot) { try { bot.quit(); } catch {} bot = null; }
      await loadModules();
      bot = mineflayer.createBot({
        host: params.host || "127.0.0.1",
        port: params.port || 25565,
        username: params.username || "SocketBot",
        auth: params.auth || "offline",
        ...(params.version ? { version: params.version } : {}),
      });
      await new Promise((resolve, reject) => {
        const t = setTimeout(() => reject(new Error("Bot spawn timeout (60s)")), 60000);
        bot.once("spawn", () => {
          clearTimeout(t);
          try { loadPlugins(bot); } catch {}
          registerBotEvents(bot);
          resolve();
        });
      });
      return { username: bot.username, host: params.host || "127.0.0.1", port: params.port || 25565 };
    }

    case "close": {
      if (bot) { try { bot.quit(); } catch {} bot = null; }
      return "Server shutting down";
    }

    // ── Observation ─────────────────────────────────────────────────
    case "observe": {
      if (!bot) throw new Error("No bot connected");
      const pos = bot.entity.position;
      return {
        health: bot.health || 20,
        food: bot.food || 20,
        position: { x: pos.x, y: pos.y, z: pos.z },
        gamemode: bot.game?.gameMode || "survival",
        time_of_day: timeOfDay(bot),
        biome: "",
        entities_nearby: nearbyEntities(bot),
        blocks_nearby: standingBlock(bot),
        players_nearby: nearbyPlayers(bot),
      };
    }

    case "get_entities": {
      if (!bot) throw new Error("No bot connected");
      return allEntities(bot);
    }

    case "get_nearby_blocks": {
      if (!bot) throw new Error("No bot connected");
      return nearbyBlockTypes(bot, params.distance || 16);
    }

    case "get_craftable": {
      if (!bot) throw new Error("No bot connected");
      try {
        const mcData = (await import("minecraft-data")).default(bot.version);
        const invCounts = {};
        for (const item of bot.inventory.items()) {
          invCounts[item.name] = (invCounts[item.name] || 0) + (item.count || 0);
        }
        const craftable = [];
        for (const [id, recipe] of Object.entries(mcData.recipes || {})) {
          if (!recipe.result) continue;
          const resultItem = mcData.items[recipe.result.id];
          if (!resultItem) continue;
          // Check if any ingredient is missing
          let canCraft = true;
          const ingredients = recipe.inShape ? recipe.inShape.flat() :
                             recipe.ingredients ? recipe.ingredients : [];
          for (const ing of ingredients) {
            if (!ing) continue;
            const ingItem = mcData.items[ing.id || ing.item?.id];
            const ingName = ingItem?.name;
            if (!ingName) continue;
            const needed = ing.count || 1;
            if ((invCounts[ingName] || 0) < needed) { canCraft = false; break; }
          }
          if (canCraft) craftable.push(resultItem.name);
        }
        return [...new Set(craftable)].slice(0, 30);
      } catch { return []; }
    }

    // ── Communication ───────────────────────────────────────────────
    case "chat": {
      if (!bot) throw new Error("No bot connected");
      bot.chat(params.message);
      return null;
    }

    // ── Movement ────────────────────────────────────────────────────
    case "move_to": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      ensureMovements(bot);
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(params.x, params.y, params.z, 1));
      return `Moving to (${Math.round(params.x)}, ${Math.round(params.y)}, ${Math.round(params.z)}).`;
    }

    case "go_to_player": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const target = findPlayer(bot, params.player_name);
      if (!target) throw new Error(`Player "${params.player_name}" not found nearby.`);
      ensureMovements(bot);
      const closeness = params.closeness || 3;
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(
        target.position.x, target.position.y, target.position.z, closeness,
      ));
      return `Moving to player ${params.player_name}.`;
    }

    case "follow_player": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const playerName = params.player_name;
      const entity = findPlayer(bot, playerName);
      if (!entity) throw new Error(`Player "${playerName}" not found nearby.`);
      ensureMovements(bot);
      const dist = params.follow_dist || 4;
      followingTarget = playerName;
      bot.pathfinder.setGoal(new pathfinder.goals.GoalFollow(entity, dist));
      return `Following ${playerName} at distance ${dist}.`;
    }

    case "move_away": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const distance = params.distance || 16;
      const angle = Math.random() * Math.PI * 2;
      const dx = Math.cos(angle) * distance;
      const dz = Math.sin(angle) * distance;
      ensureMovements(bot);
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(
        bot.entity.position.x + dx, bot.entity.position.y, bot.entity.position.z + dz, 2,
      ));
      return `Moving away by ${distance.toFixed(0)} blocks.`;
    }

    case "search_for_block": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const blockType = params.block_type;
      const range = params.search_range || 64;
      const mcData = (await import("minecraft-data")).default(bot.version);
      const bd = mcData.blocksByName[blockType];
      if (!bd) throw new Error(`Unknown block type: ${blockType}`);
      const target = bot.findBlock({ matching: bd.id, maxDistance: range });
      if (!target) throw new Error(`No ${blockType} nearby (${range} block radius).`);
      ensureMovements(bot);
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(target.position.x, target.position.y, target.position.z, 2));
      return `Moving to nearest ${blockType} at (${target.position.x}, ${target.position.y}, ${target.position.z}).`;
    }

    case "search_for_entity": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const entityType = params.entity_type;
      const range = params.search_range || 64;
      const mcData = (await import("minecraft-data")).default(bot.version);
      const eid = mcData.entitiesByName?.[entityType]?.id;
      if (!eid && entityType !== "player") throw new Error(`Unknown entity type: ${entityType}`);
      let target = null;
      for (const [, e] of Object.entries(bot.entities || {})) {
        if (!e.position) continue;
        const name = (e.name || "").toLowerCase();
        const displayName = (e.displayName || "").toLowerCase();
        const matchName = entityType.toLowerCase();
        if (name === matchName || displayName === matchName || e.type === "mob" && name.includes(matchName)) {
          const dist = bot.entity.position.distanceTo(e.position);
          if (dist <= range) { target = e; break; }
        }
      }
      if (!target) throw new Error(`No ${entityType} nearby (${range} block radius).`);
      ensureMovements(bot);
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(target.position.x, target.position.y, target.position.z, 2));
      return `Moving to nearest ${entityType}.`;
    }

    case "go_to_bed": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const bed = bot.findBlock({
        matching: (block) => block.name.includes("bed") && bot.blockAt(block.position.offset(0, 1, 0)).name === "air",
        maxDistance: 64,
      });
      if (!bed) throw new Error("No bed found nearby (64 block radius).");
      ensureMovements(bot);
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(bed.position.x, bed.position.y, bed.position.z, 2));
      await new Promise((resolve) => {
        const check = setInterval(() => {
          if (bot.pathfinder.goal === null) { clearInterval(check); resolve(); }
        }, 500);
        setTimeout(() => { clearInterval(check); resolve(); }, 30000);
      });
      try { await bot.sleep(bed); } catch (e) { throw new Error("Could not sleep: " + e.message); }
      return "Sleeping in bed.";
    }

    case "go_to_surface": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const surface = bot.findBlock({
        matching: (block) => block.name === "air",
        maxDistance: 256,
        useExtraInfo: true,
      });
      if (surface) {
        const below = bot.blockAt(surface.position.offset(0, -1, 0));
        if (below && below.name !== "air") {
          ensureMovements(bot);
          bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(surface.position.x, surface.position.y, surface.position.z, 1));
          return "Moving to surface.";
        }
      }
      // Fallback: go up
      const y = bot.entity.position.y + 50;
      ensureMovements(bot);
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(bot.entity.position.x, y, bot.entity.position.z, 1));
      return "Moving upward to surface.";
    }

    case "dig_down": {
      if (!bot) throw new Error("No bot connected");
      const maxDistance = params.distance || 10;
      // Move to center of block
      const startX = Math.floor(bot.entity.position.x) + 0.5;
      const startZ = Math.floor(bot.entity.position.z) + 0.5;
      await bot.pathfinder.goto(new pathfinder.goals.GoalNear(startX, bot.entity.position.y, startZ, 0.3));
      bot.clearControlStates();
      for (let i = 0; i < maxDistance; i++) {
        const blockBelow = bot.blockAt(new vec3(startX, bot.entity.position.y - 1, startZ));
        if (!blockBelow || blockBelow.name === "air") break;
        if (blockBelow.name === "bedrock") break;
        if (["lava", "water"].includes(blockBelow.name)) break;
        const blockHead = bot.blockAt(new vec3(startX, bot.entity.position.y + 1, startZ));
        if (blockHead && blockHead.name === "lava") break;
        await bot.dig(blockBelow);
      }
      return `Dug down ${maxDistance} blocks max.`;
    }

    case "stay": {
      if (!bot) throw new Error("No bot connected");
      const seconds = params.seconds || -1;
      if (bot.pathfinder) bot.pathfinder.stop();
      stayingUntil = seconds < 0 ? Infinity : Date.now() + seconds * 1000;
      followingTarget = null;
      return seconds < 0 ? "Staying in place indefinitely." : `Staying for ${seconds}s.`;
    }

    case "set_control_state": {
      if (!bot) throw new Error("No bot connected");
      bot.setControlState(params.control, params.state ?? true);
      return null;
    }

    // ── Combat ──────────────────────────────────────────────────────
    case "attack": {
      if (!bot) throw new Error("No bot connected");
      const entityType = params.entity_type;
      let target = null;
      for (const [, e] of Object.entries(bot.entities || {})) {
        if (!e.position) continue;
        const name = (e.name || "").toLowerCase();
        if (name === entityType.toLowerCase() && e.type === "hostile") {
          const dist = bot.entity.position.distanceTo(e.position);
          if (dist <= (params.range || 16)) { target = e; break; }
        }
      }
      if (!target) throw new Error(`No hostile ${entityType} nearby.`);
      bot.pvp.attack(target);
      return `Attacking ${entityType}.`;
    }

    case "attack_player": {
      if (!bot) throw new Error("No bot connected");
      const playerName = params.player_name;
      const playerEntity = findPlayer(bot, playerName);
      if (!playerEntity) throw new Error(`Player "${playerName}" not found.`);
      bot.pvp.attack(playerEntity);
      return `Attacking player ${playerName}.`;
    }

    // ── Inventory ───────────────────────────────────────────────────
    case "get_inventory": {
      if (!bot) throw new Error("No bot connected");
      return inventoryItems(bot);
    }

    case "get_goal": {
      if (!bot) throw new Error("No bot connected");
      const goal = bot.pathfinder?.goal;
      return goal ? `Current goal: ${goal.constructor.name}` : "No current pathfinding goal.";
    }

    case "equip": {
      if (!bot) throw new Error("No bot connected");
      const itemName = params.item_name;
      const item = bot.inventory.items().find((i) => i.name === itemName);
      if (!item) throw new Error(`No ${itemName} in inventory.`);
      const destination = params.destination || "hand";
      await bot.equip(item, destination);
      return `Equipped ${itemName} to ${destination}.`;
    }

    case "consume": {
      if (!bot) throw new Error("No bot connected");
      const itemName = params.item_name;
      const item = bot.inventory.items().find((i) => i.name === itemName);
      if (!item) throw new Error(`No ${itemName} in inventory.`);
      await bot.equip(item, "hand");
      await bot.consume();
      return `Consumed ${itemName}.`;
    }

    case "discard": {
      if (!bot) throw new Error("No bot connected");
      const itemName = params.item_name;
      const num = params.num || -1;
      let discarded = 0;
      for (const item of bot.inventory.items()) {
        if (item.name === itemName) {
          const count = num < 0 ? item.count : Math.min(item.count, num - discarded);
          await bot.toss(item.type, null, count);
          discarded += count;
          if (num > 0 && discarded >= num) break;
        }
      }
      return `Discarded ${discarded}x ${itemName}.`;
    }

    // ── Block interaction ───────────────────────────────────────────
    case "place_block": {
      if (!bot) throw new Error("No bot connected");
      const blockType = params.block_type;
      const item = bot.inventory.items().find((i) => i.name === blockType);
      if (!item) throw new Error(`No ${blockType} in inventory.`);
      await bot.equip(item, "hand");
      const refBlock = bot.blockAt(bot.entity.position.offset(0, -1, 0));
      if (!refBlock) throw new Error("No reference block to place against.");
      await bot.placeBlock(refBlock, new vec3(0, 1, 0));
      return `Placed ${blockType}.`;
    }

    case "activate_block": {
      if (!bot) throw new Error("No bot connected");
      const blockType = params.block_type;
      const mcData = (await import("minecraft-data")).default(bot.version);
      const bd = mcData.blocksByName[blockType];
      if (!bd) throw new Error(`Unknown block type: ${blockType}`);
      const target = bot.findBlock({ matching: bd.id, maxDistance: 8 });
      if (!target) throw new Error(`No ${blockType} nearby (8 block radius).`);
      await bot.activateBlock(target);
      return `Activated ${blockType}.`;
    }

    case "use_on": {
      if (!bot) throw new Error("No bot connected");
      const toolName = params.tool_name;
      const targetName = params.target;
      // Equip the tool
      if (toolName && toolName !== "hand" && toolName !== "nothing") {
        const item = bot.inventory.items().find((i) => i.name === toolName);
        if (!item) throw new Error(`No ${toolName} in inventory.`);
        await bot.equip(item, "hand");
      }
      // Find target
      if (targetName === "nothing" || targetName === "hand") {
        bot.activateItem();
        return `Used ${toolName} in hand.`;
      }
      // Try entity target
      for (const [, e] of Object.entries(bot.entities || {})) {
        if ((e.name || "").toLowerCase() === targetName.toLowerCase() && e.position) {
          const dist = bot.entity.position.distanceTo(e.position);
          if (dist <= 6) {
            await bot.useOn(e);
            return `Used ${toolName} on entity ${targetName}.`;
          }
        }
      }
      // Try block target
      const mcData = (await import("minecraft-data")).default(bot.version);
      const bd = mcData.blocksByName[targetName];
      if (bd) {
        const blk = bot.findBlock({ matching: bd.id, maxDistance: 8 });
        if (blk) {
          await bot.activateBlock(blk);
          return `Used ${toolName} on block ${targetName}.`;
        }
      }
      throw new Error(`Target "${targetName}" not found.`);
    }

    case "collect_blocks": {
      if (!bot) throw new Error("No bot connected");
      const { block_type: blockType, count } = params;
      const mcData = (await import("minecraft-data")).default(bot.version);
      const bd = mcData.blocksByName[blockType];
      if (!bd) throw new Error(`Unknown block type: ${blockType}`);
      const target = bot.findBlock({ matching: bd.id, maxDistance: 32 });
      if (!target) throw new Error(`No ${blockType} nearby (32 block radius).`);
      bot.collectBlock.collect(target);
      return `Collecting ${count || 1}x ${blockType}.`;
    }

    // ── Chest operations ────────────────────────────────────────────
    case "view_chest": {
      if (!bot) throw new Error("No bot connected");
      const chest = bot.findBlock({
        matching: ["chest", "trapped_chest", "ender_chest", "barrel", "shulker_box"].map(
          (n) => { try { const mcData = requireCache("minecraft-data"); return mcData(bot.version).blocksByName[n]?.id; } catch { return 0; } }
        ).filter(Boolean),
        maxDistance: 16,
      });
      if (!chest) throw new Error("No chest nearby.");
      const container = await bot.openContainer(chest);
      const items = container.containerItems().map((i) => `${i.name} x${i.count}`);
      container.close();
      return items.length ? items.join(", ") : "Chest is empty.";
    }

    case "put_in_chest": {
      if (!bot) throw new Error("No bot connected");
      const itemName = params.item_name;
      const num = params.num || -1;
      const chest = bot.findBlock({
        matching: (block) => ["chest", "trapped_chest", "barrel"].includes(block.name),
        maxDistance: 8,
      });
      if (!chest) throw new Error("No chest nearby (8 block radius).");
      const container = await bot.openContainer(chest);
      let moved = 0;
      for (const item of container.containerItems()) {
        if (item.name === itemName) {
          const count = num < 0 ? item.count : Math.min(item.count, num - moved);
          try { await container.withdraw(item.type, null, count); } catch {}
          moved += count;
          if (num > 0 && moved >= num) break;
        }
      }
      container.close();
      return `Stored ${moved}x ${itemName} in chest.`;
    }

    case "take_from_chest": {
      if (!bot) throw new Error("No bot connected");
      const itemName = params.item_name;
      const num = params.num || -1;
      const chest = bot.findBlock({
        matching: (block) => ["chest", "trapped_chest", "barrel"].includes(block.name),
        maxDistance: 8,
      });
      if (!chest) throw new Error("No chest nearby.");
      const container = await bot.openContainer(chest);
      let taken = 0;
      for (const item of container.containerItems()) {
        if (item.name === itemName) {
          const count = num < 0 ? item.count : Math.min(item.count, num - taken);
          try { await container.withdraw(item.type, null, count); } catch {}
          taken += count;
          if (num > 0 && taken >= num) break;
        }
      }
      container.close();
      return `Took ${taken}x ${itemName} from chest.`;
    }

    // ── Crafting / Smelting ─────────────────────────────────────────
    case "craft_recipe": {
      if (!bot) throw new Error("No bot connected");
      const recipeName = params.recipe_name;
      const num = params.num || 1;
      const mcData = (await import("minecraft-data")).default(bot.version);
      // Find recipe
      const recipes = bot.recipesAll(mcData.itemsByName[recipeName]?.id, null);
      if (!recipes.length) throw new Error(`No recipe for ${recipeName}.`);
      const recipe = recipes[0];
      // Check for crafting table
      const needsTable = recipe.requiresTable;
      let craftingTable = null;
      if (needsTable) {
        craftingTable = bot.findBlock({
          matching: mcData.blocksByName.crafting_table?.id,
          maxDistance: 8,
        });
        if (!craftingTable) throw new Error("No crafting table nearby. Place one first.");
      }
      try {
        await bot.craft(recipe, num, craftingTable);
        return `Crafted ${num}x ${recipeName}.`;
      } catch (e) {
        throw new Error(`Craft failed: ${e.message}`);
      }
    }

    case "smelt_item": {
      if (!bot) throw new Error("No bot connected");
      const itemName = params.item_name;
      const num = params.num || 1;
      const mcData = (await import("minecraft-data")).default(bot.version);
      // Find furnace
      let furnace = bot.findBlock({
        matching: mcData.blocksByName.furnace?.id,
        maxDistance: 8,
      });
      if (!furnace) throw new Error("No furnace nearby. Place one first.");
      const container = await bot.openContainer(furnace);
      // Add fuel if needed
      if (!container.fuelItem()) {
        const fuelItems = ["coal", "charcoal", "oak_log", "oak_planks"];
        for (const fuelName of fuelItems) {
          const fuelItem = mcData.itemsByName[fuelName];
          if (fuelItem) {
            try {
              await container.putFuel(fuelItem.id, null, 1);
              break;
            } catch {}
          }
        }
      }
      // Add input
      const inputItem = mcData.itemsByName[itemName];
      if (!inputItem) throw new Error(`Unknown item: ${itemName}`);
      await container.putInput(inputItem.id, null, num);
      container.close();
      return `Started smelting ${num}x ${itemName}. Check furnace later.`;
    }

    case "clear_furnace": {
      if (!bot) throw new Error("No bot connected");
      const mcData = (await import("minecraft-data")).default(bot.version);
      const furnace = bot.findBlock({
        matching: mcData.blocksByName.furnace?.id,
        maxDistance: 8,
      });
      if (!furnace) throw new Error("No furnace nearby.");
      const container = await bot.openContainer(furnace);
      try { if (container.outputItem()) await container.takeOutput(); } catch {}
      try { if (container.inputItem()) await container.takeInput(); } catch {}
      try { if (container.fuelItem()) await container.takeFuel(); } catch {}
      container.close();
      return "Furnace cleared.";
    }

    // ── Villager trading ────────────────────────────────────────────
    case "show_villager_trades": {
      if (!bot) throw new Error("No bot connected");
      const villagerId = params.villager_id;
      const villager = bot.entities[villagerId];
      if (!villager) throw new Error(`Villager #${villagerId} not found.`);
      await bot.lookAt(villager.position);
      const mcData = (await import("minecraft-data")).default(bot.version);
      const trades = villager.trades || [];
      if (!trades.length) return "Villager has no trades.";
      const lines = trades.map((t, i) => {
        const input1 = mcData.items[t.inputItem1?.type || t.firstInput?.item?.id]?.name || "?";
        const input2 = t.inputItem2 ? mcData.items[t.inputItem2?.type]?.name : null;
        const output = mcData.items[t.outputItem?.type || t.output?.item?.id]?.name || "?";
        const desc = input2 ? `${input1} + ${input2}` : input1;
        return `  #${i + 1}: ${desc} → ${output} x${t.outputItem?.count || t.output?.count || 1}`;
      });
      return "Villager trades:\n" + lines.join("\n");
    }

    case "trade_with_villager": {
      if (!bot) throw new Error("No bot connected");
      const villagerId = params.villager_id;
      const tradeIndex = (params.trade_index || 1) - 1;
      const count = params.count || 1;
      const villager = bot.entities[villagerId];
      if (!villager) throw new Error(`Villager #${villagerId} not found.`);
      const trades = villager.trades || [];
      if (!trades[tradeIndex]) throw new Error(`Trade #${params.trade_index} not available.`);
      await bot.trade(villager, tradeIndex, count);
      return `Traded x${count} with villager #${villagerId}.`;
    }

    // ── Look ────────────────────────────────────────────────────────
    case "look_at_player": {
      if (!bot) throw new Error("No bot connected");
      const playerEntity = findPlayer(bot, params.player_name);
      if (!playerEntity) throw new Error(`Player "${params.player_name}" not found.`);
      await bot.lookAt(playerEntity.position);
      return `Looking at ${params.player_name}.`;
    }

    case "look_at_position": {
      if (!bot) throw new Error("No bot connected");
      await bot.lookAt(new vec3(params.x, params.y, params.z));
      return `Looking at (${params.x}, ${params.y}, ${params.z}).`;
    }

    // ── Give ────────────────────────────────────────────────────────
    case "give_to_player": {
      if (!bot) throw new Error("No bot connected");
      const playerName = params.player_name;
      const itemName = params.item_name;
      const num = params.num || 1;
      const playerEntity = findPlayer(bot, playerName);
      if (!playerEntity) throw new Error(`Player "${playerName}" not found.`);
      // Navigate to player
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      ensureMovements(bot);
      const pp = playerEntity.position;
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(pp.x, pp.y, pp.z, 3));
      await new Promise((resolve) => {
        const check = setInterval(() => {
          if (!bot.pathfinder.goal) { clearInterval(check); resolve(); }
          const dist = bot.entity.position.distanceTo(pp);
          if (dist <= 4) { clearInterval(check); resolve(); }
        }, 500);
        setTimeout(() => { clearInterval(check); resolve(); }, 30000);
      });
      // Toss items
      let tossed = 0;
      for (const item of bot.inventory.items()) {
        if (item.name === itemName) {
          const count = Math.min(item.count, num - tossed);
          await bot.toss(item.type, null, count);
          tossed += count;
          if (tossed >= num) break;
        }
      }
      return `Gave ${tossed}x ${itemName} to ${playerName}.`;
    }

    // ── Skill: Pickup Items ──────────────────────────────────────────
    case "pickup_nearby_items": {
      if (!bot) throw new Error("No bot connected");
      const pickupDist = params.distance || 8;
      let pickedUp = 0;
      const getNearestItem = () => {
        let nearest = null;
        let nearestDist = pickupDist;
        for (const [, e] of Object.entries(bot.entities || {})) {
          if (e.name === "item" && e.position) {
            const dist = bot.entity.position.distanceTo(e.position);
            if (dist < nearestDist) { nearest = e; nearestDist = dist; }
          }
        }
        return nearest;
      };
      let nearestItem = getNearestItem();
      while (nearestItem) {
        const movements = new pathfinder.Movements(bot);
        movements.canDig = false;
        bot.pathfinder.setMovements(movements);
        await bot.pathfinder.goto(new pathfinder.goals.GoalFollow(nearestItem, 1), true);
        await new Promise((resolve) => setTimeout(resolve, 200));
        const prev = nearestItem;
        nearestItem = getNearestItem();
        if (prev === nearestItem) break;
        pickedUp++;
      }
      return `Picked up ${pickedUp} items.`;
    }

    // ── Skill: Torch Placement ──────────────────────────────────────
    case "should_place_torch": {
      if (!bot) throw new Error("No bot connected");
      const nearestTorch = bot.findBlock({
        matching: (block) => block.name === "torch" || block.name === "wall_torch",
        maxDistance: 6,
      });
      if (nearestTorch) return { should_place: false, reason: "Torch already nearby." };
      const block = bot.blockAt(bot.entity.position);
      const hasTorch = bot.inventory.items().some((i) => i.name === "torch");
      const canPlace = hasTorch && block?.name === "air";
      return { should_place: canPlace, has_torch: hasTorch, block_name: block?.name || "unknown" };
    }

    case "place_torch": {
      if (!bot) throw new Error("No bot connected");
      const torch = bot.inventory.items().find((i) => i.name === "torch");
      if (!torch) throw new Error("No torch in inventory.");
      await bot.equip(torch, "hand");
      const refBlock = bot.blockAt(bot.entity.position.offset(0, -1, 0));
      if (!refBlock) throw new Error("No reference block to place against.");
      await bot.placeBlock(refBlock, new vec3(0, 1, 0));
      return "Placed torch.";
    }

    // ── Skill: Farming ──────────────────────────────────────────────
    case "till_and_sow": {
      if (!bot) throw new Error("No bot connected");
      const x = Math.floor(params.x);
      const y = Math.floor(params.y);
      const z = Math.floor(params.z);
      const seedType = params.seed_type || null;
      const pos = new vec3(x, y, z);
      const block = bot.blockAt(pos);
      if (block.name !== "grass_block" && block.name !== "dirt" && block.name !== "farmland") {
        throw new Error(`Cannot till ${block.name}, must be grass_block, dirt, or farmland.`);
      }
      const above = bot.blockAt(new vec3(x, y + 1, z));
      if (above.name !== "air") {
        throw new Error(`Cannot till, ${above.name} is above the block.`);
      }
      // Till if not farmland
      if (block.name !== "farmland") {
        const hoes = bot.inventory.items().filter((i) => i.name.includes("hoe"));
        if (hoes.length === 0) throw new Error("Cannot till, no hoes in inventory.");
        await bot.equip(hoes[0], "hand");
        await bot.activateBlock(block);
      }
      // Plant seeds
      if (seedType) {
        let seedName = seedType;
        if (seedName.endsWith("seed") && !seedName.endsWith("seeds")) seedName += "s";
        const seedItem = bot.inventory.items().find((i) => i.name === seedName);
        if (!seedItem) throw new Error(`No ${seedName} to plant.`);
        await bot.equip(seedItem, "hand");
        await bot.activateBlock(block);
        return `Tilled and planted ${seedName} at (${x}, ${y}, ${z}).`;
      }
      return `Tilled block at (${x}, ${y}, ${z}).`;
    }

    // ── Skill: Door ─────────────────────────────────────────────────
    case "use_door": {
      if (!bot) throw new Error("No bot connected");
      const doorTypes = ["oak_door", "spruce_door", "birch_door", "jungle_door",
                         "acacia_door", "dark_oak_door", "mangrove_door", "cherry_door",
                         "bamboo_door", "crimson_door", "warped_door", "iron_door"];
      let doorBlock = null;
      if (params.x != null) {
        doorBlock = bot.blockAt(new vec3(params.x, params.y, params.z));
      } else {
        for (const dt of doorTypes) {
          const mcData = (await import("minecraft-data")).default(bot.version);
          const bd = mcData.blocksByName[dt];
          if (!bd) continue;
          doorBlock = bot.findBlock({ matching: bd.id, maxDistance: 16 });
          if (doorBlock) break;
        }
      }
      if (!doorBlock) throw new Error("No door found nearby.");
      // Navigate to door
      bot.pathfinder.setMovements(new pathfinder.Movements(bot));
      await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
        doorBlock.position.x, doorBlock.position.y, doorBlock.position.z, 1,
      ), true);
      await new Promise((resolve) => setTimeout(resolve, 1000));
      await bot.lookAt(doorBlock.position);
      // Open door if closed
      if (!doorBlock._properties?.open) {
        await bot.activateBlock(doorBlock);
      }
      // Walk through
      bot.setControlState("forward", true);
      await new Promise((resolve) => setTimeout(resolve, 600));
      bot.setControlState("forward", false);
      await bot.activateBlock(doorBlock);
      return `Used door at (${doorBlock.position.x}, ${doorBlock.position.y}, ${doorBlock.position.z}).`;
    }

    // ── Skill: Crafting Plan ────────────────────────────────────────
    case "get_crafting_plan": {
      if (!bot) throw new Error("No bot connected");
      const targetItem = params.target_item;
      const quantity = params.quantity || 1;
      // Build current inventory counts
      const currInventory = {};
      for (const item of bot.inventory.items()) {
        currInventory[item.name] = (currInventory[item.name] || 0) + (item.count || 0);
      }
      let prefix = "";
      const existing = currInventory[targetItem] || 0;
      if (existing > 0) {
        currInventory[targetItem] -= existing;
        prefix = `You already have ${existing} ${targetItem}. If you need more:\n`;
      }
      try {
        const mc = await import("../../utils/mcdata.js");
        const plan = mc.getDetailedCraftingPlan(targetItem, quantity, currInventory);
        return prefix + plan;
      } catch (e) {
        throw new Error(`Crafting plan failed: ${e.message}`);
      }
    }

    // ── Stop ────────────────────────────────────────────────────────
    case "stop": {
      if (!bot) throw new Error("No bot connected");
      followingTarget = null;
      stayingUntil = 0;
      try { bot.collectBlock.cancelTask(); } catch {}
      try { bot.pathfinder.stop(); } catch {}
      try { bot.pvp.stop(); } catch {}
      try { bot.clearControlStates(); } catch {}
      return "All actions stopped.";
    }

    default:
      throw new Error(`Unknown method: ${method}`);
  }
}

// ── TCP server ───────────────────────────────────────────────────────

const server = net.createServer((socket) => {
  if (clientSocket) {
    socket.write(
      JSON.stringify({ event: "error", data: { message: "Another client is already connected" } }) + "\n",
    );
    socket.end();
    return;
  }
  clientSocket = socket;
  console.log(`[socket-server] Client connected from ${socket.remoteAddress}:${socket.remotePort}`);

  socket.on("data", async (chunk) => {
    lineBuffer += chunk.toString();
    const lines = lineBuffer.split("\n");
    lineBuffer = lines.pop();

    for (const line of lines) {
      if (!line.trim()) continue;

      let req;
      try { req = JSON.parse(line); } catch {
        socket.write(JSON.stringify({ id: null, ok: false, error: "Invalid JSON" }) + "\n");
        continue;
      }

      const { id, method, params } = req;
      try {
        const result = await handleRequest(method, params || {});
        socket.write(JSON.stringify({ id, ok: true, result }) + "\n");
      } catch (err) {
        socket.write(JSON.stringify({ id, ok: false, error: err.message }) + "\n");
      }

      if (method === "close") {
        socket.end();
      }
    }
  });

  socket.on("close", () => {
    console.log("[socket-server] Client disconnected");
    clientSocket = null;
    setTimeout(() => {
      if (!clientSocket) {
        console.log("[socket-server] Shutting down after disconnect");
        if (bot) { try { bot.quit(); } catch {} bot = null; }
        server.close();
        process.exit(0);
      }
    }, 5000);
  });

  socket.on("error", (err) => {
    console.error("[socket-server] Socket error:", err.message);
    clientSocket = null;
  });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[socket-server] Listening on tcp://127.0.0.1:${PORT}`);
});

server.on("error", (err) => {
  console.error("[socket-server] Server error:", err.message);
  process.exit(1);
});
