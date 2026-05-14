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

let mineflayer, pathfinder, collectBlock, pvp, armorManager, autoEat, vec3, mcUtils;

async function loadModules() {
  if (mineflayer) return;
  mineflayer = (await import("mineflayer")).default;
  pathfinder = (await import("mineflayer-pathfinder")).default;
  collectBlock = (await import("mineflayer-collectblock")).default;
  pvp = (await import("mineflayer-pvp")).default;
  armorManager = (await import("mineflayer-armor-manager")).default;
  autoEat = (await import("mineflayer-auto-eat")).default;
  vec3 = (await import("vec3")).default;
  mcUtils = await import("../utils/mcdata.js");
}

function loadPlugins(b) {
  b.loadPlugin(pathfinder.pathfinder);
  b.loadPlugin(collectBlock.plugin);
  b.loadPlugin(pvp.plugin);
  b.loadPlugin(armorManager);
  b.loadPlugin(autoEat);
  if (b.autoEat?.options) {
    b.autoEat.options.offhand = false;
  }
}

// ── Bot state ────────────────────────────────────────────────────────

let bot = null;
let clientSocket = null;
let lineBuffer = "";
let followingTarget = null;   // player name being followed
let stayingUntil = 0;         // timestamp when stay() ends (0 = not staying)
let actionOutput = [];        // detailed action log, flushed to Python on demand

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function logAction(message) {
  const line = `[${new Date().toISOString()}] ${message}`;
  actionOutput.push(line);
  if (actionOutput.length > 200) actionOutput = actionOutput.slice(-200);
  console.log(`[action] ${message}`);
}

function flushActionOutput() {
  const out = actionOutput.join("\n");
  actionOutput = [];
  return out;
}

function positionSummary(b) {
  const p = b.entity?.position;
  if (!p) return "(unknown)";
  return `(${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)})`;
}

function inventoryCounts(b) {
  const counts = {};
  for (const item of inventoryItems(b)) counts[item.name] = (counts[item.name] || 0) + item.count;
  return counts;
}

function inventoryDelta(before, after) {
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  const parts = [];
  for (const key of [...keys].sort()) {
    const diff = (after[key] || 0) - (before[key] || 0);
    if (diff !== 0) parts.push(`${key} ${diff > 0 ? "+" : ""}${diff}`);
  }
  return parts.length ? parts.join(", ") : "no inventory change";
}

function inventorySummary(counts) {
  const parts = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, count]) => `${name} x${count}`);
  return parts.length ? parts.join(", ") : "empty";
}

function isAirLike(block) {
  return !block || block.name === "air" || block.name === "cave_air" || block.name === "void_air";
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function fuelSmeltsPerItem(name) {
  if (name === "lava_bucket") return 100;
  if (name === "coal" || name === "charcoal") return 8;
  if (name === "blaze_rod") return 12;
  if (name.endsWith("_log") || name.endsWith("_stem") || name.endsWith("_hyphae")) return 1.5;
  if (name.endsWith("_planks")) return 1.5;
  if (name.endsWith("_slab")) return 0.75;
  if (name === "stick" || name.endsWith("_sapling")) return 0.5;
  if (name.startsWith("wooden_")) return 1;
  return 0;
}

function fuelPreference(name) {
  if (name === "coal" || name === "charcoal") return 1;
  if (name === "stick" || name.endsWith("_sapling")) return 2;
  if (name.endsWith("_planks") || name.endsWith("_slab")) return 3;
  if (name.endsWith("_log") || name.endsWith("_stem") || name.endsWith("_hyphae")) return 4;
  if (name === "lava_bucket" || name === "blaze_rod") return 5;
  if (name.startsWith("wooden_")) return 50;
  return 10;
}

function smeltInputNameAliases(name) {
  return {
    raw_porkchop: "porkchop",
    raw_beef: "beef",
    raw_chicken: "chicken",
    raw_mutton: "mutton",
    raw_rabbit: "rabbit",
    raw_cod: "cod",
    raw_salmon: "salmon",
  }[name] || name;
}

function clampSearchRange(value, fallback = 64) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(Math.max(1, n), 64);
}

function bestSmeltingFuel(b, neededItems) {
  try {
    const candidates = b.inventory.items()
      .map((item) => ({ item, smelts: fuelSmeltsPerItem(item.name) }))
      .filter(({ item, smelts }) => smelts > 0 && item.type != null)
      .map(({ item, smelts }) => ({
        item,
        smelts,
        countNeeded: Math.ceil(neededItems / smelts),
      }))
      .filter(({ item, countNeeded }) => item.count >= countNeeded)
      .sort((a, b) =>
        fuelPreference(a.item.name) - fuelPreference(b.item.name) ||
        a.countNeeded - b.countNeeded ||
        b.smelts - a.smelts
      );
    return candidates[0] || null;
  } catch {
    return null;
  }
}

/**
 * Place a block with a configurable timeout for the blockUpdate confirmation.
 * mineflayer's built-in placeBlock hardcodes 500ms which is too short for
 * laggy servers.  This wraps _genericPlace and waits for blockUpdate with
 * a caller-controlled timeout (default 5000ms).
 */
async function placeBlockWithTimeout(ref, face, timeoutMs = 5000) {
  const dest = ref.position.plus(face);
  await bot._genericPlace(ref, face, {});

  return new Promise((resolve, reject) => {
    const eventName = `blockUpdate:${dest}`;
    const timer = setTimeout(() => {
      bot.removeListener(eventName, handler);
      reject(new Error(`Event ${eventName} did not fire within timeout of ${timeoutMs}ms`));
    }, timeoutMs);

    const handler = (oldBlock, newBlock) => {
      if (!oldBlock || !newBlock || oldBlock.type !== newBlock.type) {
        clearTimeout(timer);
        bot.removeListener(eventName, handler);
        resolve([oldBlock, newBlock]);
      }
    };
    bot.on(eventName, handler);
  });
}

async function placeBlockAndVerify(ref, face, targetPos, blockName) {
  // Single attempt — if the server rejects this position, let the
  // caller (placeInventoryBlockNearBot) try a different one instead
  // of retrying the same coordinates.
  let lastError = null;
  try {
    await placeBlockWithTimeout(ref, face, 5000);
  } catch (e) {
    lastError = e;
    const message = String(e?.message || e);
    if (!message.includes("blockUpdate") && !message.includes("timeout")) {
      throw e;
    }
  }

  // Poll after the (possibly-timed-out) placement — on laggy servers
  // the block may appear a moment after our timeout fires.
  for (let i = 0; i < 8; i++) {
    const placed = bot.blockAt(targetPos);
    if (placed?.name === blockName) return placed;
    await sleepMs(250);
  }

  throw lastError || new Error(`Could not verify placed ${blockName}.`);
}

async function placeInventoryBlockNearBot(blockName) {
  if (!bot) throw new Error("No bot connected");
  const item = bot.inventory.items().find((i) => i.name === blockName);
  if (!item) throw new Error(`No ${blockName} in inventory.`);
  await bot.equip(item, "hand");

  const base = bot.entity.position.floored();
  const errors = [];
  let feetTarget = null;

  // Prefer the block directly underfoot (needed for pillaring / elevating).
  // If the bot is standing on the target spot the server will reject direct
  // placement — jump to clear the position and retry immediately so the
  // search loop below doesn't steal the block for a non-pillaring spot.
  const underfoot = bot.blockAt(base.offset(0, -1, 0));
  if (underfoot && !isAirLike(underfoot) && !underfoot.name.includes("water") && !underfoot.name.includes("lava")) {
    feetTarget = underfoot.position.offset(0, 1, 0);
    if (isAirLike(bot.blockAt(feetTarget))) {
      try {
        return await placeBlockAndVerify(underfoot, new vec3(0, 1, 0), feetTarget, blockName);
      } catch (e) {
        errors.push(`underfoot ${feetTarget}: ${e.message}`);
        try {
          bot.setControlState('jump', true);
          await sleepMs(150);
          bot.setControlState('jump', false);
          await sleepMs(100);
          await bot.equip(item, "hand");
          return await placeBlockAndVerify(underfoot, new vec3(0, 1, 0), feetTarget, blockName);
        } catch (e2) {
          errors.push(`underfoot-jump ${feetTarget}: ${e2.message}`);
        }
      }
    }
  }

  // Search a 5×3×5 area, trying every valid position until one succeeds.
  // Track tried target positions so we don't waste attempts on duplicates.
  const tried = new Set();
  tried.add(feetTarget?.toString());  // underfoot target already attempted

  const faces = [
    new vec3(0, 1, 0),
    new vec3(1, 0, 0),
    new vec3(-1, 0, 0),
    new vec3(0, 0, 1),
    new vec3(0, 0, -1),
  ];

  for (let dy = -1; dy <= 1; dy++) {
    for (let dx = -2; dx <= 2; dx++) {
      for (let dz = -2; dz <= 2; dz++) {
        const ref = bot.blockAt(base.offset(dx, dy, dz));
        if (!ref || isAirLike(ref) || ref.name.includes("water") || ref.name.includes("lava")) continue;
        for (const face of faces) {
          const targetPos = ref.position.offset(face.x, face.y, face.z);
          if (bot.entity.position.distanceTo(targetPos) > 4.5) continue;
          // Can't place a block where the bot is standing.
          if (targetPos.equals(base)) continue;
          const key = targetPos.toString();
          if (tried.has(key)) continue;
          const target = bot.blockAt(targetPos);
          if (!isAirLike(target)) continue;
          tried.add(key);
          try {
            await bot.equip(item, "hand");
            return await placeBlockAndVerify(ref, face, targetPos, blockName);
          } catch (e) {
            errors.push(`${targetPos}: ${e.message}`);
          }
        }
      }
    }
  }
  // Last resort: jump to clear the feet position, then place there.
  const groundBlock = bot.blockAt(base.offset(0, -1, 0));
  if (groundBlock && !isAirLike(groundBlock) && !groundBlock.name.includes("water") && !groundBlock.name.includes("lava")) {
    try {
      bot.setControlState('jump', true);
      await sleepMs(150);
      bot.setControlState('jump', false);
      await sleepMs(100);
      await bot.equip(item, "hand");
      return await placeBlockAndVerify(groundBlock, new vec3(0, 1, 0), base, blockName);
    } catch (e) {
      errors.push(`jump-place ${base}: ${e.message}`);
    }
  }

  throw new Error(`Could not place ${blockName}. Tried: ${errors.join("; ") || "no valid positions"}`);
}

function recipeRequirementText(recipe, mcData) {
  try {
    const ingredients = recipe.inShape ? recipe.inShape.flat() :
      recipe.ingredients ? recipe.ingredients : [];
    const counts = {};
    for (const ing of ingredients) {
      if (!ing) continue;
      const id = ing.id ?? ing.item?.id ?? ing.type;
      const item = mcData.items[id] || mcData.blocks[id];
      const name = item?.name;
      if (!name) continue;
      counts[name] = (counts[name] || 0) + (ing.count || 1);
    }
    const text = inventorySummary(counts);
    return text === "empty" ? "unknown ingredients" : text;
  } catch {
    return "unknown ingredients";
  }
}

async function waitForPathfinderDone(timeoutMs = 60000, target = null, range = 2) {
  if (!bot?.pathfinder) return "pathfinder unavailable";
  const started = Date.now();
  return await new Promise((resolve, reject) => {
    const done = (message) => {
      cleanup();
      resolve(message);
    };
    const fail = (err) => {
      cleanup();
      reject(err instanceof Error ? err : new Error(String(err)));
    };
    const onGoal = () => done("goal reached");
    const onPathReset = (reason) => {
      if (reason && reason !== "goal_updated") done(`path reset: ${reason}`);
    };
    const cleanup = () => {
      clearInterval(interval);
      clearTimeout(timer);
      try { bot.removeListener("goal_reached", onGoal); } catch {}
      try { bot.removeListener("path_reset", onPathReset); } catch {}
    };
    const interval = setInterval(() => {
      if (target && bot.entity?.position?.distanceTo(target) <= range) done("arrived within range");
      else if (!bot.pathfinder.goal) done("no current goal");
    }, 250);
    const timer = setTimeout(() => fail(new Error(`Action timed out after ${timeoutMs}ms`)), timeoutMs);
    bot.once("goal_reached", onGoal);
    bot.once("path_reset", onPathReset);
    if (!bot.pathfinder.goal) done("no current goal");
  });
}

async function gotoAndSummarize(label, goal, targetPos = null, range = 2, timeoutMs = 60000) {
  if (!bot) throw new Error("No bot connected");
  if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
  ensureMovements(bot);
  const beforePos = positionSummary(bot);
  const beforeInv = inventoryCounts(bot);
  logAction(`${label}: started at ${beforePos}`);
  bot.pathfinder.setGoal(goal);
  const waitResult = await waitForPathfinderDone(timeoutMs, targetPos, range);
  const afterPos = positionSummary(bot);
  const afterInv = inventoryCounts(bot);
  const delta = inventoryDelta(beforeInv, afterInv);
  const detail = flushActionOutput();
  return [
    `${label}: ${waitResult}.`,
    `Position: ${beforePos} -> ${afterPos}.`,
    `Inventory: ${delta}.`,
    detail ? `Output:\n${detail}` : "",
  ].filter(Boolean).join("\n");
}

// ── Fuzzy name matching ────────────────────────────────────────────────

function _levenshtein(a, b) {
  const m = a.length, n = b.length;
  const dp = new Array(m + 1);
  for (let i = 0; i <= m; i++) { dp[i] = new Array(n + 1); dp[i][0] = i; }
  for (let j = 0; j <= n; j++) dp[0][j] = j;
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = a[i - 1] === b[j - 1]
        ? dp[i - 1][j - 1]
        : Math.min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1]) + 1;
    }
  }
  return dp[m][n];
}

/**
 * Fuzzy-match a name against a lookup map (e.g. mcData.itemsByName).
 * Returns the best-matching key or null if nothing is close enough.
 */
function _fuzzyLookup(name, lookupMap) {
  if (name == null) return null;
  const target = String(name).toLowerCase();
  const tokens = target.split("_").filter(t => t.length > 1);
  if (!tokens.length || !lookupMap) return null;

  const candidates = Object.keys(lookupMap);
  let best = null, bestScore = -Infinity;
  for (const key of candidates) {
    const keyTokens = new Set(key.split("_"));
    const tokenHits = tokens.filter(t => keyTokens.has(t)).length;
    const maxLen = Math.max(target.length, key.length);
    const distRatio = 1 - _levenshtein(target, key) / maxLen;
    const score = tokenHits + distRatio;  // whole-token hits dominate
    if (score > bestScore) { bestScore = score; best = key; }
  }

  if (best && bestScore > 0.4) return best;
  return null;
}

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

function entityDistance(b, entity) {
  const dx = b.entity.position.x - entity.position.x;
  const dy = b.entity.position.y - entity.position.y;
  const dz = b.entity.position.z - entity.position.z;
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

function entityDisplayName(entity) {
  return entity.name || entity.username || "unknown";
}

function entityMatchesType(entity, entityType) {
  const wanted = String(entityType || "").toLowerCase();
  if (!wanted) return true;
  return entityDisplayName(entity).toLowerCase() === wanted ||
    String(entity.type || "").toLowerCase() === wanted ||
    String(entity.mobType || "").toLowerCase() === wanted ||
    String(entity.username || "").toLowerCase() === wanted;
}

function nearbyEntities(b) {
  try {
    const candidates = [];
    const counts = {};
    for (const [eid, entity] of Object.entries(b.entities || {})) {
      if (entity.name && entity.name !== "player" && entity.position) {
        const name = entityDisplayName(entity);
        const dist = entityDistance(b, entity);
        counts[name] = (counts[name] || 0) + 1;
        candidates.push({ eid, entity, name, dist });
      }
    }
    candidates.sort((a, b2) => a.dist - b2.dist);
    const parts = [];
    const countParts = Object.entries(counts)
      .sort((a, b2) => b2[1] - a[1] || a[0].localeCompare(b2[0]))
      .slice(0, 12)
      .map(([name, count]) => `${name} x${count}`);
    if (countParts.length) parts.push(`counts: ${countParts.join(", ")}`);
    for (const item of candidates.slice(0, 10)) {
      parts.push(`  ${item.name} (${item.dist.toFixed(0)}m) [id:${item.eid}]`);
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
    const botPos = b.entity?.position;
    if (botPos) {
      result.sort((a, b2) => {
        const da = Math.sqrt(
          ((botPos.x - a.position.x) ** 2) +
          ((botPos.y - a.position.y) ** 2) +
          ((botPos.z - a.position.z) ** 2),
        );
        const db = Math.sqrt(
          ((botPos.x - b2.position.x) ** 2) +
          ((botPos.y - b2.position.y) ** 2) +
          ((botPos.z - b2.position.z) ** 2),
        );
        return da - db;
      });
    }
    return result;
  } catch { return []; }
}

function entityCounts(b, distance = 64) {
  try {
    const counts = {};
    for (const [, e] of Object.entries(b.entities || {})) {
      if (!e.position) continue;
      const dist = entityDistance(b, e);
      if (dist > distance) continue;
      const name = entityDisplayName(e);
      if (name === "player") continue;
      counts[name] = (counts[name] || 0) + 1;
    }
    return counts;
  } catch { return {}; }
}

function countEntities(b, entityType, distance = 64) {
  try {
    const matches = [];
    for (const [, e] of Object.entries(b.entities || {})) {
      if (!e.position || !entityMatchesType(e, entityType)) continue;
      const dist = entityDistance(b, e);
      if (dist > distance) continue;
      matches.push({
        id: e.id || 0,
        name: entityDisplayName(e),
        type: e.type || "mob",
        distance: Number(dist.toFixed(2)),
        position: { x: e.position.x, y: e.position.y, z: e.position.z },
        health: e.health ?? 0,
      });
    }
    matches.sort((a, b2) => a.distance - b2.distance);
    return {
      entity_type: entityType || "",
      range: distance,
      count: matches.length,
      entities: matches.slice(0, 20),
    };
  } catch {
    return { entity_type: entityType || "", range: distance, count: 0, entities: [] };
  }
}

function nearbyBlockTypes(b, distance = 16) {
  try {
    const types = new Set();
    const pos = b.entity.position;
    const mcData = b.registry;
    for (const name of [
      "crafting_table",
      "furnace",
      "campfire",
      "blast_furnace",
      "smoker",
      "chest",
    ]) {
      const id = mcData?.blocksByName?.[name]?.id;
      if (id == null) continue;
      const found = b.findBlock({ matching: id, maxDistance: distance });
      if (found) types.add(name);
    }
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
  const wanted = String(username || "").toLowerCase();
  for (const [name, p] of Object.entries(b.players || {})) {
    if (name.toLowerCase() === wanted && p?.entity) return p.entity;
  }
  return null;
}

function playerNotTrackedMessage(username) {
  return `Player "${username}" is not currently tracked by the server. ` +
    "They may be in another dimension, offline, or not loaded by the bot yet.";
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
          try { mcUtils.initMinecraftData(bot.version); } catch (e) {
            console.warn(`[socket] mcdata init failed for ${bot.version}: ${e.message}`);
          }
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
        entity_counts: entityCounts(bot, 64),
        blocks_nearby: standingBlock(bot),
        players_nearby: nearbyPlayers(bot),
      };
    }

    case "get_entities": {
      if (!bot) throw new Error("No bot connected");
      return allEntities(bot);
    }

    case "count_entities": {
      if (!bot) throw new Error("No bot connected");
      return countEntities(bot, params.entity_type, clampSearchRange(params.range, 64));
    }

    case "get_nearby_blocks": {
      if (!bot) throw new Error("No bot connected");
      return nearbyBlockTypes(bot, clampSearchRange(params.distance, 16));
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
      const target = new vec3(params.x, params.y, params.z);
      return await gotoAndSummarize(
        `move_to (${Math.round(params.x)}, ${Math.round(params.y)}, ${Math.round(params.z)})`,
        new pathfinder.goals.GoalNear(params.x, params.y, params.z, 1),
        target,
        1.5,
      );
    }

    case "go_to_player": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const target = findPlayer(bot, params.player_name);
      if (!target) throw new Error(playerNotTrackedMessage(params.player_name));
      const closeness = params.closeness || 3;
      return await gotoAndSummarize(
        `go_to_player ${params.player_name}`,
        new pathfinder.goals.GoalNear(
          target.position.x, target.position.y, target.position.z, closeness,
        ),
        target.position,
        closeness + 0.5,
      );
    }

    case "follow_player": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const playerName = params.player_name;
      const entity = findPlayer(bot, playerName);
      if (!entity) throw new Error(playerNotTrackedMessage(playerName));
      ensureMovements(bot);
      const dist = params.follow_dist || 4;
      followingTarget = playerName;
      // IMPORTANT: pass dynamic=true as the second arg so the goal is
      // re-evaluated when the target moves. Without this, GoalFollow is
      // a one-shot pathfind to the player's position at this instant and
      // the bot will stop once it arrives, even if the player walks away.
      // (Mirrors JS mindcraft library/skills.js:1349.)
      bot.pathfinder.setGoal(
        new pathfinder.goals.GoalFollow(entity, dist),
        true,
      );
      return `Following ${playerName} at distance ${dist}.`;
    }

    case "move_away": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const distance = params.distance || 16;
      const angle = Math.random() * Math.PI * 2;
      const dx = Math.cos(angle) * distance;
      const dz = Math.sin(angle) * distance;
      const target = new vec3(
        bot.entity.position.x + dx,
        bot.entity.position.y,
        bot.entity.position.z + dz,
      );
      return await gotoAndSummarize(
        `move_away ${distance.toFixed(0)} blocks`,
        new pathfinder.goals.GoalNear(target.x, target.y, target.z, 2),
        target,
        3,
      );
    }

    case "search_for_block": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const blockType = params.block_type;
      const range = clampSearchRange(params.search_range);
      const mcData = (await import("minecraft-data")).default(bot.version);
      const bd = mcData.blocksByName[blockType];
      if (!bd) throw new Error(`Unknown block type: ${blockType}`);
      const target = bot.findBlock({ matching: bd.id, maxDistance: range });
      if (!target) throw new Error(`No ${blockType} nearby (${range} block radius).`);
      return await gotoAndSummarize(
        `search_for_block ${blockType} at (${target.position.x}, ${target.position.y}, ${target.position.z})`,
        new pathfinder.goals.GoalNear(target.position.x, target.position.y, target.position.z, 2),
        target.position,
        2.5,
      );
    }

    case "search_for_entity": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const entityType = params.entity_type;
      const range = clampSearchRange(params.search_range);
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
      return await gotoAndSummarize(
        `search_for_entity ${entityType}`,
        new pathfinder.goals.GoalNear(target.position.x, target.position.y, target.position.z, 2),
        target.position,
        2.5,
      );
    }

    case "go_to_bed": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const bed = bot.findBlock({
        matching: (block) => block.name.includes("bed") && bot.blockAt(block.position.offset(0, 1, 0)).name === "air",
        maxDistance: 64,
      });
      if (!bed) throw new Error("No bed found nearby (64 block radius).");
      await gotoAndSummarize(
        `go_to_bed at (${bed.position.x}, ${bed.position.y}, ${bed.position.z})`,
        new pathfinder.goals.GoalNear(bed.position.x, bed.position.y, bed.position.z, 2),
        bed.position,
        2.5,
        30000,
      );
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
          return await gotoAndSummarize(
            "go_to_surface",
            new pathfinder.goals.GoalNear(surface.position.x, surface.position.y, surface.position.z, 1),
            surface.position,
            1.5,
          );
        }
      }
      // Fallback: go up
      const y = bot.entity.position.y + 50;
      const target = new vec3(bot.entity.position.x, y, bot.entity.position.z);
      return await gotoAndSummarize(
        "go_to_surface upward fallback",
        new pathfinder.goals.GoalNear(target.x, target.y, target.z, 1),
        target,
        1.5,
      );
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
      const range = clampSearchRange(params.range);
      let target = null;
      for (const [, e] of Object.entries(bot.entities || {})) {
        if (!e.position) continue;
        const name = (e.name || "").toLowerCase();
        const displayName = (e.displayName || "").toLowerCase();
        const matchName = entityType.toLowerCase();
        if (name === matchName || displayName === matchName) {
          const dist = bot.entity.position.distanceTo(e.position);
          if (dist <= range) { target = e; break; }
        }
      }
      if (!target) throw new Error(`No ${entityType} nearby (${range} block radius).`);
      bot.pvp.attack(target);
      const started = Date.now();
      while (bot.entities?.[target.id] && Date.now() - started < 30000) {
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      try { bot.pvp.stop(); } catch {}
      try { await bot.collectBlock.collect(bot.nearestEntity((e) => e.name === "item")); } catch {}
      return bot.entities?.[target.id] ? `Attacking ${entityType}.` : `Killed ${entityType} and picked up nearby drops.`;
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
      await placeInventoryBlockNearBot(blockType);
      return `Placed ${blockType}.`;
    }

    case "activate_block": {
      if (!bot) throw new Error("No bot connected");
      const blockType = params.block_type;
      const mcData = (await import("minecraft-data")).default(bot.version);
      const bd = mcData.blocksByName[blockType];
      if (!bd) throw new Error(`Unknown block type: ${blockType}`);
      const target = bot.findBlock({ matching: bd.id, maxDistance: 64 });
      if (!target) throw new Error(`No ${blockType} nearby (64 block radius).`);
      if (bot.entity.position.distanceTo(target.position) > 4) {
        await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
          target.position.x,
          target.position.y,
          target.position.z,
          4,
        ));
      }
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
        const blk = bot.findBlock({ matching: bd.id, maxDistance: 64 });
        if (blk) {
          if (bot.entity.position.distanceTo(blk.position) > 4) {
            await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
              blk.position.x,
              blk.position.y,
              blk.position.z,
              4,
            ));
          }
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
      const wanted = count || 8;
      const positions = bot.findBlocks({ matching: bd.id, maxDistance: 64, count: wanted });
      if (!positions.length) throw new Error(`No ${blockType} nearby (64 block radius).`);
      const targets = positions
        .map((pos) => bot.blockAt(pos))
        .filter((block) => block && block.name === blockType)
        .slice(0, wanted);
      if (!targets.length) throw new Error(`No reachable ${blockType} targets found.`);
      // Check if bot has the required tool before attempting to collect
      const requiredTool = mcUtils.getBlockTool(blockType);
      if (requiredTool) {
        const hasTool = bot.inventory.items().some((i) => i.name === requiredTool);
        if (!hasTool) {
          throw new Error(
            `Need a ${requiredTool} to mine ${blockType}. ` +
            `You have: ${bot.inventory.items().map((i) => `${i.name} x${i.count}`).join(", ") || "nothing"}. ` +
            `Craft a ${requiredTool} first, then retry.`
          );
        }
      }
      const beforePos = positionSummary(bot);
      const beforeInv = inventoryCounts(bot);
      logAction(`collect_blocks ${wanted}x ${blockType}: found ${targets.length} targets`);
      await bot.collectBlock.collect(targets.length === 1 ? targets[0] : targets);
      const afterPos = positionSummary(bot);
      const afterInv = inventoryCounts(bot);
      const detail = flushActionOutput();
      return [
        `Collected ${targets.length}x ${blockType}.`,
        `Position: ${beforePos} -> ${afterPos}.`,
        `Inventory: ${inventoryDelta(beforeInv, afterInv)}.`,
        detail ? `Output:\n${detail}` : "",
      ].filter(Boolean).join("\n");
    }

    case "break_nearby_blocks": {
      if (!bot) throw new Error("No bot connected");
      const { block_type: blockType } = params;
      const wanted = Math.max(1, Number(params.count || 1));
      const searchRange = Math.min(clampSearchRange(params.range, 6), 16);
      const mcData = (await import("minecraft-data")).default(bot.version);
      const bd = mcData.blocksByName[blockType];
      if (!bd) throw new Error(`Unknown block type: ${blockType}`);

      const beforePos = positionSummary(bot);
      const beforeInv = inventoryCounts(bot);
      const positions = bot.findBlocks({
        matching: bd.id,
        maxDistance: searchRange,
        count: wanted * 4,
      });
      const targets = [];
      for (const pos of positions) {
        const block = bot.blockAt(pos);
        if (!block || block.name !== blockType) continue;
        const distance = bot.entity.position.distanceTo(block.position.offset(0.5, 0.5, 0.5));
        if (distance > 4.75) {
          logAction(
            `break_nearby_blocks ${blockType}: skipped ${pos} at distance ${distance.toFixed(1)}`,
          );
          continue;
        }
        if (typeof bot.canDigBlock === "function" && !bot.canDigBlock(block)) {
          logAction(`break_nearby_blocks ${blockType}: cannot dig ${pos}`);
          continue;
        }
        targets.push(block);
        if (targets.length >= wanted) break;
      }
      if (!targets.length) {
        throw new Error(`No breakable ${blockType} within ${searchRange} blocks and reach.`);
      }

      let broken = 0;
      logAction(`break_nearby_blocks ${wanted}x ${blockType}: found ${targets.length} reachable targets`);
      for (const block of targets) {
        const current = bot.blockAt(block.position);
        if (!current || current.name !== blockType) continue;
        await bot.lookAt(current.position.offset(0.5, 0.5, 0.5), true);
        await bot.dig(current);
        broken++;
        await sleepMs(150);
      }

      let pickupResult = "";
      try {
        pickupResult = await handleRequest("pickup_nearby_items", {
          distance: Math.max(4, searchRange + 2),
        });
      } catch (err) {
        pickupResult = `Pickup skipped: ${err.message || String(err)}`;
      }
      const afterPos = positionSummary(bot);
      const afterInv = inventoryCounts(bot);
      const detail = flushActionOutput();
      return [
        `Broke ${broken}x ${blockType}.`,
        pickupResult,
        `Position: ${beforePos} -> ${afterPos}.`,
        `Inventory: ${inventoryDelta(beforeInv, afterInv)}.`,
        detail ? `Output:\n${detail}` : "",
      ].filter(Boolean).join("\n");
    }

    // ── Chest operations ────────────────────────────────────────────
    case "view_chest": {
      if (!bot) throw new Error("No bot connected");
      const chest = bot.findBlock({
        matching: ["chest", "trapped_chest", "ender_chest", "barrel", "shulker_box"].map(
          (n) => { try { const mcData = requireCache("minecraft-data"); return mcData(bot.version).blocksByName[n]?.id; } catch { return 0; } }
        ).filter(Boolean),
        maxDistance: 64,
      });
      if (!chest) throw new Error("No chest nearby.");
      if (bot.entity.position.distanceTo(chest.position) > 4) {
        await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
          chest.position.x,
          chest.position.y,
          chest.position.z,
          4,
        ));
      }
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
        maxDistance: 64,
      });
      if (!chest) throw new Error("No chest nearby (64 block radius).");
      if (bot.entity.position.distanceTo(chest.position) > 4) {
        await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
          chest.position.x,
          chest.position.y,
          chest.position.z,
          4,
        ));
      }
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
        maxDistance: 64,
      });
      if (!chest) throw new Error("No chest nearby.");
      if (bot.entity.position.distanceTo(chest.position) > 4) {
        await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
          chest.position.x,
          chest.position.y,
          chest.position.z,
          4,
        ));
      }
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
      const recipeName = String(params.recipe_name || "").trim();
      if (!recipeName) throw new Error("No recipe_name provided.");
      const num = params.num || 1;
      const mcData = (await import("minecraft-data")).default(bot.version);
      // Find recipe — exact match first, then fuzzy fallback
      let itemId = mcData.itemsByName[recipeName]?.id;
      let resolvedName = recipeName;
      if (itemId == null) {
        const fuzzy = _fuzzyLookup(recipeName, mcData.itemsByName);
        if (fuzzy) {
          console.warn(`[socket] Fuzzy matched "${recipeName}" → "${fuzzy}"`);
          itemId = mcData.itemsByName[fuzzy]?.id;
          resolvedName = fuzzy;
        }
      }
      if (itemId == null) throw new Error(`No recipe for ${recipeName}.`);

      const beforePos = positionSummary(bot);
      const beforeInv = inventoryCounts(bot);
      const notes = [];
      let placedTable = false;
      let craftingTable = null;

      let recipes = bot.recipesFor(itemId, null, 1, null);
      if (!recipes.length) {
        craftingTable = bot.findBlock({
          matching: mcData.blocksByName.crafting_table?.id,
          maxDistance: 64,
        });
        if (!craftingTable) {
          const hasTable = bot.inventory.items().some((i) => i.name === "crafting_table");
          if (hasTable) {
            craftingTable = await placeInventoryBlockNearBot("crafting_table");
            placedTable = true;
            notes.push(`Placed crafting_table at (${craftingTable.position.x}, ${craftingTable.position.y}, ${craftingTable.position.z}).`);
          }
        }
        if (craftingTable) {
          if (bot.entity.position.distanceTo(craftingTable.position) > 4) {
            await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
              craftingTable.position.x,
              craftingTable.position.y,
              craftingTable.position.z,
              4,
            ));
          }
          recipes = bot.recipesFor(itemId, null, 1, craftingTable);
        }
      }

      if (!recipes.length) {
        const tableRecipes = bot.recipesFor(itemId, null, 1, true);
        if (tableRecipes.length && !craftingTable) {
          throw new Error(`Crafting ${resolvedName} requires a crafting_table. You do not have one nearby or in inventory.`);
        }
        const allRecipes = bot.recipesAll(itemId, null);
        const req = allRecipes.length ? recipeRequirementText(allRecipes[0], mcData) : "unknown ingredients";
        throw new Error(
          `Missing ingredient for ${resolvedName}. Required recipe includes: ${req}. ` +
          `Inventory: ${inventorySummary(beforeInv)}.`,
        );
      }

      const recipe = recipes[0];
      try {
        await bot.craft(recipe, num, craftingTable);
        notes.push(`Crafted ${num}x ${resolvedName}.`);
        if (placedTable && craftingTable) {
          try {
            await bot.collectBlock.collect(craftingTable);
            notes.push("Picked crafting_table back up.");
          } catch (e) {
            notes.push(`Could not pick crafting_table back up: ${e.message}`);
          }
        }
        const afterPos = positionSummary(bot);
        const afterInv = inventoryCounts(bot);
        return [
          ...notes,
          `Position: ${beforePos} -> ${afterPos}.`,
          `Inventory: ${inventoryDelta(beforeInv, afterInv)}.`,
        ].join("\n");
      } catch (e) {
        console.error(`[socket] craft_recipe ${resolvedName} failed`, e?.stack || e);
        throw new Error(
          `Craft failed for ${resolvedName}: ${e.message}. ` +
          `Inventory: ${inventorySummary(inventoryCounts(bot))}.`,
        );
      }
    }

    case "smelt_item": {
      if (!bot) throw new Error("No bot connected");
      const requestedItemName = String(params.item_name || "");
      const itemName = smeltInputNameAliases(requestedItemName);
      const num = params.num || 1;
      const mcData = (await import("minecraft-data")).default(bot.version);
      const registry = bot.registry || mcData;
      const itemsByName = registry.itemsByName || mcData.itemsByName;
      const blocksByName = registry.blocksByName || mcData.blocksByName;
      const beforePos = positionSummary(bot);
      const beforeInv = inventoryCounts(bot);
      let inputItem = itemsByName[itemName];
      if (!inputItem) {
        const fuzzy = _fuzzyLookup(itemName, itemsByName);
        if (fuzzy) {
          console.warn(`[socket] Fuzzy matched "${requestedItemName}" → "${fuzzy}"`);
          inputItem = itemsByName[fuzzy];
        }
      }
      if (!inputItem) throw new Error(`Unknown item: ${requestedItemName}`);
      const inputStacks = bot.inventory.items()
        .filter((item) => item.name === inputItem.name || item.type === inputItem.id);
      const availableInput = inputStacks.reduce((total, item) => total + item.count, 0);
      if (availableInput < num) {
        throw new Error(`Not enough ${requestedItemName}: have ${availableInput}, need ${num}.`);
      }
      // Mineflayer transfer() validates against bot.registry. Use the live
      // inventory stack type when available; minecraft-data ids can diverge
      // across protocol/version mappings and trigger "Invalid itemType".
      const inputType = inputStacks[0].type;
      // Find furnace
      let furnace = bot.findBlock({
        matching: blocksByName.furnace?.id,
        maxDistance: 64,
      });
      if (!furnace) throw new Error("No furnace nearby. Place one first.");
      if (bot.entity.position.distanceTo(furnace.position) > 4) {
        await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
          furnace.position.x,
          furnace.position.y,
          furnace.position.z,
          4,
        ));
        furnace = bot.blockAt(furnace.position);
        if (!furnace || furnace.name !== "furnace") {
          throw new Error("Furnace moved or disappeared before smelting.");
        }
      }
      const furnaceWindow = await bot.openFurnace(furnace);
      let total = 0;
      const clearedInputItems = [];
      try {
        const currentInput = furnaceWindow.inputItem();
        if (currentInput && currentInput.type !== inputType && currentInput.name !== inputItem.name) {
          const takenInput = await furnaceWindow.takeInput();
          if (takenInput) {
            clearedInputItems.push(`${takenInput.count}x ${takenInput.name || takenInput.displayName}`);
          } else {
            throw new Error(`Furnace already has ${currentInput.name || currentInput.displayName} as input.`);
          }
        }
        if (!furnaceWindow.fuelItem()) {
          const fuel = bestSmeltingFuel(bot, num);
          if (!fuel) {
            throw new Error(
              `No fuel for smelting ${requestedItemName}. Need coal, charcoal, logs, planks, sticks, or another fuel.`,
            );
          }
          await furnaceWindow.putFuel(fuel.item.type, null, fuel.countNeeded);
        }
        await furnaceWindow.putInput(inputType, null, num);
        const idleDeadlineMs = 12000;
        let lastOutputAt = Date.now();
        while (total < num && Date.now() - lastOutputAt < idleDeadlineMs) {
          await sleepMs(1000);
          const output = furnaceWindow.outputItem();
          if (!output) continue;
          const taken = await furnaceWindow.takeOutput();
          if (taken) {
            total += taken.count;
            lastOutputAt = Date.now();
          }
        }
      } finally {
        try { await bot.closeWindow(furnaceWindow); } catch {}
      }
      const afterPos = positionSummary(bot);
      const afterInv = inventoryCounts(bot);
      if (total <= 0) {
        throw new Error(`Failed to smelt ${requestedItemName}; no output was produced.`);
      }
      return [
        clearedInputItems.length
          ? `Cleared furnace input: ${clearedInputItems.join(", ")}.`
          : "",
        `Smelted ${total}x ${requestedItemName}.`,
        `Position: ${beforePos} -> ${afterPos}.`,
        `Inventory: ${inventoryDelta(beforeInv, afterInv)}.`,
      ].join("\n");
    }

    case "clear_furnace": {
      if (!bot) throw new Error("No bot connected");
      const mcData = (await import("minecraft-data")).default(bot.version);
      const furnace = bot.findBlock({
        matching: mcData.blocksByName.furnace?.id,
        maxDistance: 64,
      });
      if (!furnace) throw new Error("No furnace nearby.");
      if (bot.entity.position.distanceTo(furnace.position) > 4) {
        await bot.pathfinder.goto(new pathfinder.goals.GoalNear(
          furnace.position.x,
          furnace.position.y,
          furnace.position.z,
          4,
        ));
      }
      const container = await bot.openFurnace(furnace);
      try { if (container.outputItem()) await container.takeOutput(); } catch {}
      try { if (container.inputItem()) await container.takeInput(); } catch {}
      try { if (container.fuelItem()) await container.takeFuel(); } catch {}
      try { await bot.closeWindow(container); } catch {}
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
      const num = params.num || 8;
      const playerEntity = findPlayer(bot, playerName);
      if (!playerEntity) throw new Error(playerNotTrackedMessage(playerName));
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
      if (tossed <= 0) {
        throw new Error(`Could not give ${itemName}: tossed 0 items.`);
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
          doorBlock = bot.findBlock({ matching: bd.id, maxDistance: 64 });
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
        mcUtils.ensureMinecraftData(bot.version);
        const plan = mcUtils.getDetailedCraftingPlan(targetItem, quantity, currInventory);
        return prefix + plan;
      } catch (e) {
        throw new Error(`Crafting plan failed: ${e.message}`);
      }
    }

    // ── Dynamic code / action output ───────────────────────────────
    case "wait_for_action": {
      if (!bot) throw new Error("No bot connected");
      const result = await waitForPathfinderDone(params.timeout_ms || 60000);
      const detail = flushActionOutput();
      return detail ? `${result}\n${detail}` : result;
    }

    case "get_action_output": {
      return flushActionOutput();
    }

    case "execute_code": {
      if (!bot) throw new Error("No bot connected");
      const code = String(params.code || "").trim();
      if (!code) throw new Error("No code provided.");
      const timeoutMs = params.timeout_ms || 60000;
      const localOutput = [];
      const log = (...args) => {
        const msg = args.map((arg) => {
          if (typeof arg === "string") return arg;
          try { return JSON.stringify(arg); } catch { return String(arg); }
        }).join(" ");
        localOutput.push(msg);
        logAction(`code: ${msg}`);
      };
      const api = {
        chat: async (message) => handleRequest("chat", { message }),
        observe: async () => handleRequest("observe", {}),
        inventory: async () => handleRequest("get_inventory", {}),
        entities: async () => handleRequest("get_entities", {}),
        countEntities: async (entity_type, range = 64) =>
          handleRequest("count_entities", { entity_type, range }),
        moveTo: async (x, y, z) => handleRequest("move_to", { x, y, z }),
        goToPlayer: async (player_name, closeness = 3) =>
          handleRequest("go_to_player", { player_name, closeness }),
        searchForBlock: async (block_type, search_range = 64) =>
          handleRequest("search_for_block", { block_type, search_range }),
        searchForEntity: async (entity_type, search_range = 64) =>
          handleRequest("search_for_entity", { entity_type, search_range }),
        collectBlocks: async (block_type, count = 1) =>
          handleRequest("collect_blocks", { block_type, count }),
        breakNearbyBlocks: async (block_type, count = 1, range = 6) =>
          handleRequest("break_nearby_blocks", { block_type, count, range }),
        pickupNearbyItems: async (distance = 8) =>
          handleRequest("pickup_nearby_items", { distance }),
        attack: async (entity_type, range = 16) =>
          handleRequest("attack", { entity_type, range }),
        equip: async (item_name, destination = "hand") =>
          handleRequest("equip", { item_name, destination }),
        placeBlock: async (block_type) => handleRequest("place_block", { block_type }),
        waitForAction: async (ms = 60000) => handleRequest("wait_for_action", { timeout_ms: ms }),
        stop: async () => handleRequest("stop", {}),
        log,
      };
      const beforePos = positionSummary(bot);
      const beforeInv = inventoryCounts(bot);
      logAction(`execute_code: started at ${beforePos}`);
      try {
        const fn = new AsyncFunction("bot", "api", "Vec3", "log", code);
        const result = await Promise.race([
          fn(bot, api, vec3, log),
          new Promise((_, reject) => setTimeout(
            () => reject(new Error(`Dynamic code timed out after ${timeoutMs}ms`)),
            timeoutMs,
          )),
        ]);
        const afterPos = positionSummary(bot);
        const afterInv = inventoryCounts(bot);
        const detail = flushActionOutput();
        return {
          ok: true,
          result: result == null ? "" : String(result),
          output: [
            ...localOutput,
            `Position: ${beforePos} -> ${afterPos}.`,
            `Inventory: ${inventoryDelta(beforeInv, afterInv)}.`,
            detail,
          ].filter(Boolean).join("\n"),
        };
      } catch (e) {
        const detail = flushActionOutput();
        return {
          ok: false,
          result: "",
          output: [
            `FAILED: ${e.message}`,
            ...localOutput,
            detail,
          ].filter(Boolean).join("\n"),
        };
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
  lineBuffer = "";
  const requestQueue = [];
  let processingRequests = false;
  console.log(`[socket-server] Client connected from ${socket.remoteAddress}:${socket.remotePort}`);

  function writeResponse(payload) {
    if (socket.destroyed) return;
    try {
      socket.write(JSON.stringify(payload) + "\n");
    } catch (err) {
      console.error("[socket-server] Failed to write response:", err.message);
    }
  }

  async function processRequestQueue() {
    if (processingRequests) return;
    processingRequests = true;
    try {
      while (requestQueue.length && !socket.destroyed) {
        const { id, method, params } = requestQueue.shift();
        try {
          const result = await handleRequest(method, params || {});
          writeResponse({ id, ok: true, result });
        } catch (err) {
          writeResponse({ id, ok: false, error: err.message });
        }

        if (method === "close") {
          requestQueue.length = 0;
          socket.end();
          break;
        }
      }
    } finally {
      processingRequests = false;
    }
  }

  socket.on("data", (chunk) => {
    lineBuffer += chunk.toString();
    const lines = lineBuffer.split("\n");
    lineBuffer = lines.pop();

    for (const line of lines) {
      if (!line.trim()) continue;

      let req;
      try { req = JSON.parse(line); } catch {
        writeResponse({ id: null, ok: false, error: "Invalid JSON" });
        continue;
      }

      requestQueue.push(req);
    }
    void processRequestQueue();
  });

  socket.on("close", () => {
    console.log("[socket-server] Client disconnected");
    requestQueue.length = 0;
    lineBuffer = "";
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
    requestQueue.length = 0;
    lineBuffer = "";
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
