# Mindcraft-Py

Mindcraft-Py is a Python-first Minecraft bot built around a small Mineflayer socket bridge. The Python agent owns routing, planning, memory, RAG, command completion, and task lifecycle logic; Node.js is kept as a thin runtime layer for Mineflayer world actions.

## Runtime Shape

```text
Minecraft chat
  -> src/mc/socket_server.js
  -> src/mc/socket_bridge.py
  -> src/agent/core.py
  -> src/agent/executor.py
  -> Mineflayer action
```

The old upstream Mindcraft JS agent, model providers, process runner, and browser UI are no longer part of this codebase. The remaining JavaScript files are:

- `src/mc/socket_server.js`: the TCP JSON bridge around Mineflayer.
- `src/utils/mcdata.js`: Minecraft data helpers used by the bridge.

## Setup

Install Python dependencies:

```bash
uv sync
```

Install the Node bridge dependencies:

```bash
npm install
```

Create local config from the examples:

```bash
cp .env.example .env
```

Then edit `.env` with your Minecraft server, LLM, and optional Wiki RAG settings.

## Run

Start the Python bot:

```bash
uv run python main.py
```

`main.py` will reuse an existing socket bridge on the configured port or start `src/mc/socket_server.js` automatically.

To run only the Mineflayer bridge:

```bash
npm start -- --port 9090
```

## Useful Docs

- [Bot command flow](docs/bot-command-flow.md)
- [Command reference](docs/commands.md)
- [Manual test commands](docs/bot-manual-test-commands.md)
- [Wiki knowledge build guide](docs/how-to-build-wiki-knowledge.md)
- [Wiki RAG roadmap](roadmap/06-wiki-rag-plan.md)

## Tests

```bash
uv run pytest
```

For quick Python linting:

```bash
uv run ruff check src tests scripts
```
