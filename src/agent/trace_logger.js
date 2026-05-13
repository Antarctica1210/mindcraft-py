import { promises as fs } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const TRACE_FILE = process.env.MINDCRAFT_TRACE_FILE
    || path.join(__dirname, '../../log-js/mindcraft_trace.log');

let initialized = false;
let sequence = 0;

function round(n) {
    return typeof n === 'number' ? Math.round(n * 100) / 100 : n;
}

function summarizeBot(bot) {
    if (!bot) return null;
    const pos = bot.entity?.position;
    return {
        username: bot.username,
        position: pos ? { x: round(pos.x), y: round(pos.y), z: round(pos.z) } : null,
        health: bot.health,
        food: bot.food,
        dimension: bot.game?.dimension,
        heldItem: bot.heldItem ? { name: bot.heldItem.name, count: bot.heldItem.count, type: bot.heldItem.type } : null,
        inventory: bot.inventory?.items?.().slice(0, 40).map(item => ({
            name: item.name,
            count: item.count,
            type: item.type
        })) ?? []
    };
}

function safeValue(value, depth = 2, seen = new WeakSet()) {
    if (value == null) return value;
    if (typeof value === 'string') return value.length > 1200 ? value.slice(0, 1200) + '...' : value;
    if (typeof value === 'number' || typeof value === 'boolean') return value;
    if (typeof value === 'function') return `[Function ${value.name || 'anonymous'}]`;
    if (value instanceof Error) {
        return { name: value.name, message: value.message, stack: value.stack };
    }
    if (typeof value !== 'object') return String(value);
    if (seen.has(value)) return '[Circular]';
    seen.add(value);

    if (value.username && value.entity && value.inventory) {
        return summarizeBot(value);
    }
    if (typeof value.x === 'number' && typeof value.y === 'number' && typeof value.z === 'number') {
        return { x: round(value.x), y: round(value.y), z: round(value.z) };
    }
    if (value.name && value.position && value.boundingBox) {
        return {
            name: value.name,
            type: value.type,
            position: safeValue(value.position, depth - 1, seen)
        };
    }
    if (value.name && value.count != null && value.type != null) {
        return { name: value.name, count: value.count, type: value.type };
    }
    if (depth <= 0) return `[${value.constructor?.name || 'Object'}]`;
    if (Array.isArray(value)) {
        return value.slice(0, 25).map(item => safeValue(item, depth - 1, seen));
    }

    const out = {};
    for (const key of Object.keys(value).slice(0, 30)) {
        if (key.startsWith('_')) continue;
        out[key] = safeValue(value[key], depth - 1, seen);
    }
    return out;
}

async function ensureInitialized() {
    if (initialized) return;
    initialized = true;
    await fs.mkdir(path.dirname(TRACE_FILE), { recursive: true });
    await fs.appendFile(
        TRACE_FILE,
        JSON.stringify({
            ts: new Date().toISOString(),
            event: 'trace.session_start',
            pid: process.pid,
            cwd: process.cwd()
        }) + '\n'
    );
}

export function getTraceFile() {
    return TRACE_FILE;
}

export function trace(event, data = {}) {
    const seq = ++sequence;
    const line = JSON.stringify({
        ts: new Date().toISOString(),
        seq,
        event,
        ...safeValue(data, 3)
    }) + '\n';

    ensureInitialized()
        .then(() => fs.appendFile(TRACE_FILE, line))
        .catch(() => {});
}

export function traceBotEvent(event, bot, data = {}) {
    trace(event, {
        bot: summarizeBot(bot),
        ...data
    });
}

export async function traceAsync(event, data, fn) {
    const started = Date.now();
    trace(`${event}.start`, data);
    try {
        const result = await fn();
        trace(`${event}.done`, {
            ...data,
            elapsed_ms: Date.now() - started,
            result
        });
        return result;
    } catch (error) {
        trace(`${event}.error`, {
            ...data,
            elapsed_ms: Date.now() - started,
            error
        });
        throw error;
    }
}
