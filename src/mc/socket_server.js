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

let mineflayer, pathfinder, collectBlock, pvp, armorManager, autoEat;

async function loadModules() {
  if (mineflayer) return;
  mineflayer = (await import("mineflayer")).default;
  pathfinder = (await import("mineflayer-pathfinder")).default;
  collectBlock = (await import("mineflayer-collectblock")).default;
  pvp = (await import("mineflayer-pvp")).default;
  armorManager = (await import("mineflayer-armor-manager")).default;
  autoEat = (await import("mineflayer-auto-eat")).default;
}

function loadPlugins(bot) {
  bot.loadPlugin(pathfinder.pathfinder);
  bot.loadPlugin(collectBlock.plugin);
  bot.loadPlugin(pvp.plugin);
  bot.loadPlugin(armorManager);
  bot.loadPlugin(autoEat);
}

// ── Bot state ────────────────────────────────────────────────────────

let bot = null;
let clientSocket = null;
let lineBuffer = "";

// ── Push events to the connected Python client ───────────────────────

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
  b.on("spawn", () => emit("spawn", {}));
}

// ── Observation helpers ──────────────────────────────────────────────

function timeOfDay(b) {
  try {
    const h = (b.time.timeOfDay || 0) / 1000;
    if (h < 6) return "night";
    if (h < 7) return "sunrise";
    if (h < 12) return "day";
    if (h < 13) return "noon";
    if (h < 18) return "afternoon";
    if (h < 19) return "sunset";
    return "night";
  } catch {
    return "";
  }
}

function nearbyEntities(b) {
  try {
    const parts = [];
    for (const [, entity] of Object.entries(b.entities || {})) {
      if (parts.length >= 10) break;
      if (entity.name && entity.name !== "player" && entity.position) {
        const dx = b.entity.position.x - entity.position.x;
        const dy = b.entity.position.y - entity.position.y;
        const dz = b.entity.position.z - entity.position.z;
        const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        parts.push(`  ${entity.name} (${dist.toFixed(0)}m)`);
      }
    }
    return parts.join("\n");
  } catch {
    return "";
  }
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
  } catch {
    return "";
  }
}

function standingBlock(b) {
  try {
    const blk = b.blockAt(b.entity.position);
    return blk ? `standing on: ${blk.name}` : "";
  } catch {
    return "";
  }
}

function inventoryItems(b) {
  try {
    const items = b.inventory.items();
    if (!items?.length) return [];
    return items.slice(0, 30).map((i) => ({ name: i.name || "unknown", count: i.count || 0 }));
  } catch {
    return [];
  }
}

// ── Request handler ──────────────────────────────────────────────────

async function handleRequest(method, params) {
  switch (method) {
    case "create_bot": {
      if (bot) {
        try { bot.quit(); } catch { /* ok */ }
        bot = null;
      }
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
          try { loadPlugins(bot); } catch { /* ok */ }
          registerBotEvents(bot);
          resolve();
        });
      });

      return {
        username: bot.username,
        host: params.host || "127.0.0.1",
        port: params.port || 25565,
      };
    }

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

    case "chat": {
      if (!bot) throw new Error("No bot connected");
      bot.chat(params.message);
      return null;
    }

    case "set_control_state": {
      if (!bot) throw new Error("No bot connected");
      bot.setControlState(params.control, params.state ?? true);
      return null;
    }

    case "move_to": {
      if (!bot) throw new Error("No bot connected");
      if (!bot.pathfinder) throw new Error("Pathfinder not loaded");
      const { x, y, z } = params;
      if (!bot.pathfinder.Movements) {
        bot.pathfinder.setMovements(new pathfinder.Movements(bot));
      }
      bot.pathfinder.setGoal(new pathfinder.goals.GoalNear(x, y, z, 1));
      return `Moving to (${Math.round(x)}, ${Math.round(y)}, ${Math.round(z)}).`;
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

    case "stop": {
      if (!bot) throw new Error("No bot connected");
      try { bot.collectBlock.cancelTask(); } catch { /* ok */ }
      try { bot.pathfinder.stop(); } catch { /* ok */ }
      try { bot.pvp.stop(); } catch { /* ok */ }
      try { bot.clearControlStates(); } catch { /* ok */ }
      return "All actions stopped.";
    }

    case "get_inventory": {
      if (!bot) throw new Error("No bot connected");
      return inventoryItems(bot);
    }

    case "get_goal": {
      if (!bot) throw new Error("No bot connected");
      const goal = bot.pathfinder?.goal;
      return goal ? `Current goal: ${goal.constructor.name}` : "No current pathfinding goal.";
    }

    case "close": {
      if (bot) { try { bot.quit(); } catch { /* ok */ } bot = null; }
      return "Server shutting down";
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
      try {
        req = JSON.parse(line);
      } catch {
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
        if (bot) { try { bot.quit(); } catch { /* ok */ } bot = null; }
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
