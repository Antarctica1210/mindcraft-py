"""Command metadata used by the agent loop."""
from __future__ import annotations

import re
from dataclasses import dataclass

from llm.message_builder import CommandDef


@dataclass(frozen=True)
class CommandMeta:
    name: str
    completion_kind: str
    terminal_by_default: bool = False
    allowed_in_navigation_task: bool = False
    inventory_prep: bool = False
    requires_pre_action_observe: bool = False
    direct_shortcut: bool = False
    preflight_hooks: tuple[str, ...] = ()


COMMAND_COMPLETION_KINDS: dict[str, str] = {
    # Control / meta
    "stop": "control",
    "stfu": "control",
    "unshut": "control",
    "restart": "control",
    "clearChat": "control",
    "setMode": "control",
    "setGoal": "control",
    "endGoal": "control",
    # Queries
    "stats": "query",
    "inventory": "query",
    "goal": "query",
    "entities": "query",
    "countEntities": "query",
    "nearbyBlocks": "query",
    "craftable": "query",
    "help": "query",
    "modes": "query",
    "getCraftingPlan": "query",
    "searchWiki": "query",
    "showVillagerTrades": "query",
    "viewChest": "query",
    # Navigation / locating
    "moveTo": "navigation",
    "goToPlayer": "navigation",
    "followPlayer": "navigation",
    "goToCoordinates": "navigation",
    "searchForBlock": "navigation",
    "searchForEntity": "navigation",
    "moveAway": "navigation",
    "goToSurface": "navigation",
    "stay": "navigation",
    "lookAtPlayer": "navigation",
    "lookAtPosition": "navigation",
    # Inventory and delivery
    "equip": "inventory",
    "consume": "inventory",
    "discard": "inventory",
    "givePlayer": "delivery",
    "collectBlocks": "inventory",
    "collectAnyWood": "inventory",
    "mineOreWithTool": "inventory",
    "craftRecipe": "inventory",
    "craftWithDependencies": "inventory",
    "smeltItem": "inventory",
    "pickupNearbyItems": "inventory",
    "putInChest": "inventory",
    "takeFromChest": "inventory",
    # World changes
    "attack": "world_change",
    "attackPlayer": "world_change",
    "placeHere": "world_change",
    "activateBlock": "world_change",
    "breakNearbyBlocks": "world_change",
    "breedAnimals": "world_change",
    "breedAnimalsBatch": "world_change",
    "breedAnimalsInPenBatch": "world_change",
    "useOn": "world_change",
    "clearFurnace": "world_change",
    "tradeWithVillager": "world_change",
    "goToBed": "world_change",
    "digDown": "world_change",
    "tillAndSow": "world_change",
    "plantSeedsNearPlayer": "world_change",
    "useDoor": "world_change",
    "newAction": "custom_action",
    "startConversation": "conversation",
    "endConversation": "conversation",
}

TERMINAL_COMPLETION_KINDS: frozenset[str] = frozenset({
    "world_change",
    "custom_action",
})

ALWAYS_TERMINAL_WORLD_CHANGE_COMMANDS: frozenset[str] = frozenset({
    "activateBlock",
    "breedAnimals",
    "breedAnimalsBatch",
    "breedAnimalsInPenBatch",
    "goToBed",
    "placeHere",
    "plantSeedsNearPlayer",
    "tillAndSow",
    "tradeWithVillager",
    "useDoor",
    "useOn",
})

CONDITIONAL_TERMINAL_WORLD_CHANGE_COMMANDS: frozenset[str] = frozenset({
    "attack",
    "attackPlayer",
    "breakNearbyBlocks",
    "clearFurnace",
    "digDown",
})
WORLD_CHANGE_EVIDENCE_FALLBACK_COMMANDS: frozenset[str] = frozenset({
    "breedAnimals",
    "breakNearbyBlocks",
    "breedAnimalsInPenBatch",
    "placeHere",
    "plantSeedsNearPlayer",
    "tillAndSow",
})
QUERY_COMMANDS: frozenset[str] = frozenset(
    name for name, kind in COMMAND_COMPLETION_KINDS.items() if kind == "query"
)
PRE_ACTION_OBSERVE_COMMANDS: frozenset[str] = frozenset({
    "attack",
    "breakNearbyBlocks",
    "breedAnimals",
    "collectBlocks",
    "collectAnyWood",
    "consume",
    "craftWithDependencies",
    "craftRecipe",
    "discard",
    "equip",
    "givePlayer",
    "goToCoordinates",
    "goToPlayer",
    "moveAway",
    "moveTo",
    "placeHere",
    "searchForBlock",
    "searchForEntity",
    "smeltItem",
    "mineOreWithTool",
    "plantSeedsNearPlayer",
    "breedAnimalsBatch",
    "breedAnimalsInPenBatch",
})
DIRECT_SHORTCUT_COMMANDS: frozenset[str] = frozenset({
    "followPlayer",
    "givePlayer",
    "goToPlayer",
    "stop",
})
DIRECT_SHORTCUT_ROUTES: frozenset[str] = frozenset({
    "direct_action",
    "direct_cook",
    "direct_item",
    "skill",
})
PREFLIGHT_HOOKS: dict[str, tuple[str, ...]] = {
    "breedAnimalsInPenBatch": ("known_home_shelter_location",),
    "collectBlocks": ("knowledge_tool_requirement",),
    "mineOreWithTool": ("knowledge_tool_requirement",),
    "craftRecipe": ("skip_nearby_furnace", "bed_dye_misconception", "missing_materials"),
    "craftWithDependencies": ("skip_nearby_furnace", "bed_dye_misconception"),
    "goToCoordinates": ("coordinate_validation",),
    "givePlayer": ("delivery_preposition",),
    "moveTo": ("coordinate_validation",),
    "placeHere": ("skip_reusable_workstation",),
    "smeltItem": ("smelt_dependencies", "defer_log_charcoal"),
}

INVENTORY_PREP_COMMANDS: frozenset[str] = frozenset({
    "craftRecipe",
    "craftWithDependencies",
    "getCraftingPlan",
    "equip",
    "searchWiki",
    "inventory",
    "craftable",
})

NAVIGATION_ONLY_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "stats",
    "inventory",
    "goal",
    "entities",
    "countEntities",
    "nearbyBlocks",
    "moveTo",
    "goToPlayer",
    "followPlayer",
    "goToCoordinates",
    "moveAway",
    "stay",
    "goToSurface",
    "lookAtPlayer",
    "lookAtPosition",
})


def command_defs() -> dict[str, CommandDef]:
    """Return the full set of bot command definitions for the LLM prompt.

    Mirrors the JS command list in ``src/agent/commands/``.
    """
    defs = {
        # ── Control ─────────────────────────────────────────────────
        "stop": CommandDef(
            name="stop",
            description="Stop all current actions (movement, mining, combat, following).",
        ),
        "stfu": CommandDef(
            name="stfu",
            description="Stop chatting and self-narration, but continue current actions.",
        ),
        "unshut": CommandDef(
            name="unshut",
            description="Resume chatting and self-narration after stfu.",
        ),
        "restart": CommandDef(
            name="restart",
            description="Restart the agent process (clean shutdown).",
        ),
        "clearChat": CommandDef(
            name="clearChat",
            description="Clear the conversation history.",
        ),
        # ── Observation ─────────────────────────────────────────────
        "stats": CommandDef(
            name="stats",
            description="Report current health, food, position, gamemode, biome, time, "
            "weather, and nearby players.",
        ),
        "inventory": CommandDef(
            name="inventory",
            description="List all items in the bot's inventory with counts.",
        ),
        "goal": CommandDef(
            name="goal",
            description="Report the current pathfinding goal, if any.",
        ),
        "entities": CommandDef(
            name="entities",
            description="List nearby players and entities with positions and types.",
        ),
        "countEntities": CommandDef(
            name="countEntities",
            description="Count nearby entities of a given type within a range.",
            params={
                "entity_type": {"type": "string", "description": "Entity type, e.g. chicken"},
                "range": {"type": "int", "description": "Search range in blocks, max 64"},
            },
        ),
        "nearbyBlocks": CommandDef(
            name="nearbyBlocks",
            description="List block types near the bot.",
        ),
        "craftable": CommandDef(
            name="craftable",
            description="List all items craftable with current inventory.",
        ),
        "help": CommandDef(
            name="help",
            description="List all available commands.",
        ),
        "modes": CommandDef(
            name="modes",
            description="Show all available behavior modes and their status.",
        ),
        # ── Movement ────────────────────────────────────────────────
        "moveTo": CommandDef(
            name="moveTo",
            description="Move the bot to the given coordinates.",
            params={
                "x": {"type": "float", "description": "X coordinate"},
                "y": {"type": "float", "description": "Y coordinate"},
                "z": {"type": "float", "description": "Z coordinate"},
            },
        ),
        "goToPlayer": CommandDef(
            name="goToPlayer",
            description="Navigate to the given player.",
            params={
                "player_name": {"type": "string", "description": "Player name to go to"},
                "closeness": {"type": "float", "description": "How close to get (default 3)"},
            },
        ),
        "followPlayer": CommandDef(
            name="followPlayer",
            description="Continuously follow a player.",
            params={
                "player_name": {"type": "string", "description": "Player name to follow"},
                "follow_dist": {"type": "float", "description": "Following distance (default 4)"},
            },
        ),
        "goToCoordinates": CommandDef(
            name="goToCoordinates",
            description="Move to specific x, y, z coordinates.",
            params={
                "x": {"type": "float", "description": "X coordinate"},
                "y": {"type": "float", "description": "Y coordinate"},
                "z": {"type": "float", "description": "Z coordinate"},
                "closeness": {"type": "float", "description": "How close to get (default 2)"},
            },
        ),
        "searchForBlock": CommandDef(
            name="searchForBlock",
            description="Find and navigate to the nearest block of the given type.",
            params={
                "block_type": {
                    "type": "string",
                    "description": (
                        "Concrete block type. For generic wood, prefer whichever log type "
                        "is visible nearby, e.g. spruce_log or oak_log; do not force oak. "
                        "For wheat seeds, short_grass and tall_grass can both work; "
                        "prefer nearby short_grass before searching far for tall_grass."
                    ),
                },
                "search_range": {"type": "float", "description": "Search radius (default 64)"},
            },
        ),
        "searchForEntity": CommandDef(
            name="searchForEntity",
            description="Find and navigate to the nearest entity of the given type.",
            params={
                "entity_type": {"type": "string", "description": "Entity type, e.g. cow"},
                "search_range": {"type": "float", "description": "Search radius (default 64)"},
            },
        ),
        "moveAway": CommandDef(
            name="moveAway",
            description="Move in a random direction by the given distance.",
            params={
                "distance": {"type": "float", "description": "Distance to move (default 16)"},
            },
        ),
        "goToBed": CommandDef(
            name="goToBed",
            description="Find the nearest bed and sleep in it.",
        ),
        "goToSurface": CommandDef(
            name="goToSurface",
            description="Navigate upward to the surface.",
        ),
        "digDown": CommandDef(
            name="digDown",
            description="Dig straight down, stopping at lava/water/bedrock.",
            params={
                "distance": {"type": "int", "description": "Max distance to dig (default 10)"},
            },
        ),
        "stay": CommandDef(
            name="stay",
            description="Stay in place. Pauses all modes.",
            params={
                "seconds": {"type": "int", "description": "Seconds to stay (-1 = indefinite)"},
            },
        ),
        # ── Combat ──────────────────────────────────────────────────
        "attack": CommandDef(
            name="attack",
            description=(
                "Attack and kill the nearest entity of the given type. "
                "Works on hostile mobs (zombie, skeleton, creeper) AND on "
                "passive mobs you need to harvest (sheep for wool, cow for "
                "leather/beef, chicken for feathers/meat, pig for pork). "
                "Use this whenever you need a resource that drops from a "
                "mob — do NOT keep calling searchForEntity if the target is "
                "already nearby. Default search radius is 64 blocks."
            ),
            params={
                "entity_type": {
                    "type": "string",
                    "description": (
                        "Entity type, e.g. zombie, sheep, cow, chicken, pig"
                    ),
                },
                "range": {
                    "type": "float",
                    "description": "Search radius in blocks. Default: 64.",
                    "default": 64,
                },
            },
        ),
        "attackPlayer": CommandDef(
            name="attackPlayer",
            description="Attack a specific player until they die or flee.",
            params={
                "player_name": {"type": "string", "description": "Player name to attack"},
            },
        ),
        # ── Inventory ───────────────────────────────────────────────
        "equip": CommandDef(
            name="equip",
            description="Equip an item to the given body slot.",
            params={
                "item_name": {"type": "string", "description": "Item to equip, e.g. diamond_sword"},
            },
        ),
        "consume": CommandDef(
            name="consume",
            description="Eat or drink the given item.",
            params={
                "item_name": {"type": "string", "description": "Item to consume, e.g. cooked_beef"},
            },
        ),
        "discard": CommandDef(
            name="discard",
            description="Discard items from inventory.",
            params={
                "item_name": {"type": "string", "description": "Item to discard"},
                "num": {"type": "int", "description": "Number to discard (-1 = all)"},
            },
        ),
        "givePlayer": CommandDef(
            name="givePlayer",
            description="Navigate to a player and give them items.",
            params={
                "player_name": {"type": "string", "description": "Player to give to"},
                "item_name": {"type": "string", "description": "Item to give"},
                "num": {"type": "int", "description": "Number to give (default 8)"},
            },
        ),
        # ── Block Interaction ───────────────────────────────────────
        "placeHere": CommandDef(
            name="placeHere",
            description="Place a block from inventory at the current position.",
            params={
                "block_type": {"type": "string", "description": "Block type to place"},
            },
        ),
        "activateBlock": CommandDef(
            name="activateBlock",
            description="Right-click / activate the nearest block of the given type.",
            params={
                "block_type": {"type": "string", "description": "Block type, e.g. lever"},
            },
        ),
        "collectBlocks": CommandDef(
            name="collectBlocks",
            description="Mine and collect blocks of the specified type.",
            params={
                "block_type": {
                    "type": "string",
                    "description": (
                        "Concrete block type. For generic wood, use the nearest/available "
                        "log type such as spruce_log, birch_log, or oak_log."
                    ),
                },
                "count": {"type": "int", "description": "Number to collect (default 8)"},
            },
        ),
        "collectAnyWood": CommandDef(
            name="collectAnyWood",
            description=(
                "Batch collect any nearby/available log or stem type. Use this for generic "
                "wood requests instead of forcing oak_log."
            ),
            params={
                "count": {"type": "int", "description": "Number of logs/stems to collect"},
                "range": {
                    "type": "float",
                    "description": "Nearby block scan radius before fallback searches",
                },
            },
        ),
        "mineOreWithTool": CommandDef(
            name="mineOreWithTool",
            description=(
                "Batch mine ore while preparing the required pickaxe first when possible. "
                "Use this for ore collection tasks like iron, coal, copper, redstone, "
                "diamond, emerald, lapis, or gold."
            ),
            params={
                "ore_type": {
                    "type": "string",
                    "description": "Ore or block name, e.g. iron, iron_ore, coal_ore",
                },
                "count": {"type": "int", "description": "Number of ore blocks to mine"},
            },
        ),
        "breakNearbyBlocks": CommandDef(
            name="breakNearbyBlocks",
            description=(
                "Directly break nearby reachable fragile blocks without long pathfinding, "
                "then pick up drops. Prefer this over collectBlocks for short_grass, "
                "tall_grass, flowers, and other thin nearby blocks. For wheat seeds, "
                "short_grass is valid; do not ignore it while searching for tall_grass."
            ),
            params={
                "block_type": {"type": "string", "description": "Block type, e.g. short_grass"},
                "count": {"type": "int", "description": "Number to break (default 1)"},
                "range": {
                    "type": "float",
                    "description": "Local search radius, max 16 (default 6)",
                },
            },
        ),
        "breedAnimals": CommandDef(
            name="breedAnimals",
            description=(
                "Breed two nearby animals of the same type by feeding each the specified "
                "breeding item. Prefer this over newAction for animal breeding."
            ),
            params={
                "animal_type": {
                    "type": "string",
                    "description": "Animal type, e.g. chicken, cow, sheep, pig",
                },
                "breeding_item": {
                    "type": "string",
                    "description": "Food item to feed, e.g. wheat_seeds for chicken",
                },
                "range": {
                    "type": "float",
                    "description": "Search radius for two matching animals, max 64 (default 16)",
                },
            },
        ),
        "breedAnimalsBatch": CommandDef(
            name="breedAnimalsBatch",
            description=(
                "Breed multiple pairs of nearby animals by repeatedly feeding two adults. "
                "Use this for requests to breed several pairs instead of calling "
                "breedAnimals one pair at a time."
            ),
            params={
                "animal_type": {
                    "type": "string",
                    "description": "Animal type, e.g. chicken, cow, sheep, pig",
                },
                "pairs": {"type": "int", "description": "Number of breeding pairs"},
                "breeding_item": {
                    "type": "string",
                    "description": "Food item to feed; optional if known for the animal",
                },
                "range": {
                    "type": "float",
                    "description": "Search radius for matching animals, max 64",
                },
            },
        ),
        "breedAnimalsInPenBatch": CommandDef(
            name="breedAnimalsInPenBatch",
            description=(
                "Batch breed animals in a nearby fenced pen. This first checks for "
                "fence/fence_gate blocks nearby, then repeatedly feeds matching animals "
                "within a small pen radius. Only use this at a player-marked home, base, "
                "or shelter location."
            ),
            params={
                "animal_type": {
                    "type": "string",
                    "description": "Animal type inside the pen, e.g. chicken, cow, sheep, pig",
                },
                "pairs": {"type": "int", "description": "Number of breeding pairs"},
                "breeding_item": {
                    "type": "string",
                    "description": "Food item to feed; optional if known for the animal",
                },
                "range": {
                    "type": "float",
                    "description": "Small pen search radius, default 8, max 16",
                },
                "player_name": {
                    "type": "string",
                    "description": "Optional player/pen owner to return near before breeding",
                },
            },
        ),
        "useOn": CommandDef(
            name="useOn",
            description="Use a tool on a target entity or block.",
            params={
                "tool_name": {"type": "string", "description": "Tool to use, e.g. bucket"},
                "target": {
                    "type": "string",
                    "description": "Target entity/block or 'nothing'/'hand'",
                },
            },
        ),
        # ── Chest ───────────────────────────────────────────────────
        "viewChest": CommandDef(
            name="viewChest",
            description="Show contents of the nearest chest.",
        ),
        "putInChest": CommandDef(
            name="putInChest",
            description="Store items in the nearest chest.",
            params={
                "item_name": {"type": "string", "description": "Item to store"},
                "num": {"type": "int", "description": "Number to store (-1 = all)"},
            },
        ),
        "takeFromChest": CommandDef(
            name="takeFromChest",
            description="Take items from the nearest chest.",
            params={
                "item_name": {"type": "string", "description": "Item to take"},
                "num": {"type": "int", "description": "Number to take (-1 = all)"},
            },
        ),
        # ── Crafting / Smelting ─────────────────────────────────────
        "craftRecipe": CommandDef(
            name="craftRecipe",
            description="Craft a recipe using available materials. Automatically "
            "detects nearby crafting tables and uses them. If no table is nearby "
            "but one is in inventory, places it, crafts, then picks it back up. "
            "Some recipes (beds, tools, armor) require a placed crafting table. "
            "Always use getCraftingPlan before crafting complex items.",
            params={
                "recipe_name": {"type": "string", "description": "Item to craft, e.g. stick"},
                "num": {"type": "int", "description": "Number to craft (default 1)"},
            },
        ),
        "craftWithDependencies": CommandDef(
            name="craftWithDependencies",
            description=(
                "Batch craft an item after preparing common dependencies such as planks, "
                "sticks, crafting_table, furnace, cobblestone, and basic tools. Prefer "
                "this over manual multi-step crafting for tools and workstations."
            ),
            params={
                "recipe_name": {"type": "string", "description": "Item to craft"},
                "num": {"type": "int", "description": "Number to craft (default 1)"},
            },
        ),
        "smeltItem": CommandDef(
            name="smeltItem",
            description=(
                "Smelt items in a furnace. Requires a furnace nearby and fuel + "
                "input items in inventory. For torches, prefer mining coal_ore over "
                "smelting logs into charcoal unless charcoal was explicitly requested."
            ),
            params={
                "item_name": {"type": "string", "description": "Item to smelt, e.g. raw_iron"},
                "num": {"type": "int", "description": "Number to smelt (default 1)"},
            },
        ),
        "clearFurnace": CommandDef(
            name="clearFurnace",
            description="Clear all items from the nearest furnace.",
        ),
        # ── Villager ────────────────────────────────────────────────
        "showVillagerTrades": CommandDef(
            name="showVillagerTrades",
            description="Show trades for a villager by entity ID.",
            params={
                "villager_id": {"type": "int", "description": "Villager entity ID"},
            },
        ),
        "tradeWithVillager": CommandDef(
            name="tradeWithVillager",
            description="Execute a trade with a villager.",
            params={
                "villager_id": {"type": "int", "description": "Villager entity ID"},
                "trade_index": {"type": "int", "description": "Trade number (1-based)"},
                "count": {"type": "int", "description": "Number of trades to execute"},
            },
        ),
        # ── Look ────────────────────────────────────────────────────
        "lookAtPlayer": CommandDef(
            name="lookAtPlayer",
            description="Look at a player or in the same direction as a player.",
            params={
                "player_name": {"type": "string", "description": "Player to look at"},
            },
        ),
        "lookAtPosition": CommandDef(
            name="lookAtPosition",
            description="Look at specific coordinates.",
            params={
                "x": {"type": "float", "description": "X coordinate"},
                "y": {"type": "float", "description": "Y coordinate"},
                "z": {"type": "float", "description": "Z coordinate"},
            },
        ),
        # ── Mode / Goal ─────────────────────────────────────────────
        "setMode": CommandDef(
            name="setMode",
            description="Enable or disable a behavior mode.",
            params={
                "mode_name": {"type": "string", "description": "Mode name"},
                "on": {"type": "bool", "description": "True to enable, False to disable"},
            },
        ),
        "setGoal": CommandDef(
            name="setGoal",
            description="Set a goal message for persistent self-prompting.",
            params={
                "self_prompt": {"type": "string", "description": "Goal message for self-prompting"},
            },
        ),
        "endGoal": CommandDef(
            name="endGoal",
            description="End the current goal and stop self-prompting.",
        ),
        "newAction": CommandDef(
            name="newAction",
            description=(
                "Generate and run a one-off JavaScript Mineflayer action when "
                "built-in commands are not expressive enough."
            ),
            params={
                "code_prompt": {
                    "type": "string",
                    "description": "Natural language description of the custom action to code.",
                },
            },
        ),
        # ── Skills ──────────────────────────────────────────────────
        "pickupNearbyItems": CommandDef(
            name="pickupNearbyItems",
            description="Pick up all nearby dropped items on the ground.",
            params={
                "distance": {"type": "float", "description": "Pickup radius (default 8)"},
            },
        ),
        "tillAndSow": CommandDef(
            name="tillAndSow",
            description="Till the ground at coordinates and optionally plant seeds.",
            params={
                "x": {"type": "float", "description": "X coordinate to till"},
                "y": {"type": "float", "description": "Y coordinate to till"},
                "z": {"type": "float", "description": "Z coordinate to till"},
                "seed_type": {
                    "type": "string",
                    "description": "Seed type to plant, e.g. wheat_seeds (optional)",
                },
            },
        ),
        "plantSeedsNearPlayer": CommandDef(
            name="plantSeedsNearPlayer",
            description=(
                "Batch till and plant multiple nearby plots around the bot or a player. "
                "Use this for planting requests instead of calling tillAndSow one block "
                "at a time."
            ),
            params={
                "count": {"type": "int", "description": "Number of seeds/plots to plant"},
                "seed_type": {
                    "type": "string",
                    "description": "Seed item to plant, e.g. wheat_seeds",
                },
                "player_name": {
                    "type": "string",
                    "description": "Optional player to return near before planting",
                },
                "range": {"type": "float", "description": "Local planting radius"},
            },
        ),
        "useDoor": CommandDef(
            name="useDoor",
            description="Open a door, walk through it, and close it.",
            params={
                "x": {
                    "type": "float",
                    "description": "X coordinate of door (optional, finds nearest)",
                },
                "y": {"type": "float", "description": "Y coordinate of door (optional)"},
                "z": {"type": "float", "description": "Z coordinate of door (optional)"},
            },
        ),
        "getCraftingPlan": CommandDef(
            name="getCraftingPlan",
            description="Get a comprehensive crafting plan for a target item. Shows "
            "required ingredients with exact quantities, what you already have, "
            "and what you still need to gather. Always use this before crafting "
            "complex items to know which intermediate steps are needed.",
            params={
                "target_item": {
                    "type": "string",
                    "description": "The item to craft, e.g. diamond_sword",
                },
                "quantity": {
                    "type": "int",
                    "description": "Quantity to craft (default 1)",
                },
            },
        ),
        "searchWiki": CommandDef(
            name="searchWiki",
            description="Search the Minecraft Wiki for info about an item, block, or mechanic.",
            params={
                "query": {
                    "type": "string",
                    "description": "The search query, e.g. 'enchanting table'",
                },
            },
        ),
        # ── Conversation ────────────────────────────────────────────
        "startConversation": CommandDef(
            name="startConversation",
            description="Start a conversation with another player.",
            params={
                "player_name": {"type": "string", "description": "Player to talk to"},
                "message": {"type": "string", "description": "Message to send"},
            },
        ),
        "endConversation": CommandDef(
            name="endConversation",
            description="End the current conversation.",
            params={
                "player_name": {"type": "string", "description": "Player to stop talking to"},
            },
        ),
    }
    for name, command in defs.items():
        command.completion_kind = command_completion_kind(name)
    return defs



def command_completion_kind(command_name: str) -> str:
    return COMMAND_COMPLETION_KINDS.get(command_name.lstrip("!"), "none")


def command_meta(command_name: str) -> CommandMeta:
    name = command_name.lstrip("!")
    kind = command_completion_kind(name)
    terminal = kind in TERMINAL_COMPLETION_KINDS or name in ALWAYS_TERMINAL_WORLD_CHANGE_COMMANDS
    if name in CONDITIONAL_TERMINAL_WORLD_CHANGE_COMMANDS:
        terminal = False
    return CommandMeta(
        name=name,
        completion_kind=kind,
        terminal_by_default=terminal,
        allowed_in_navigation_task=name in NAVIGATION_ONLY_ALLOWED_COMMANDS,
        inventory_prep=name in INVENTORY_PREP_COMMANDS,
        requires_pre_action_observe=name in PRE_ACTION_OBSERVE_COMMANDS,
        direct_shortcut=name in DIRECT_SHORTCUT_COMMANDS,
        preflight_hooks=PREFLIGHT_HOOKS.get(name, ()),
    )


def command_preflight_hooks(command_name: str) -> tuple[str, ...]:
    return PREFLIGHT_HOOKS.get(command_name.lstrip("!"), ())


def shortcut_route_known(route: str) -> bool:
    return route in DIRECT_SHORTCUT_ROUTES


def world_change_command_can_complete(command_name: str, original_message: str) -> bool:
    """Return whether a world mutation is specific enough to close unknown tasks."""
    name = command_name.lstrip("!")
    if name in ALWAYS_TERMINAL_WORLD_CHANGE_COMMANDS:
        return True
    if name not in CONDITIONAL_TERMINAL_WORLD_CHANGE_COMMANDS:
        return False

    message = (original_message or "").lower()
    if name in {"attack", "attackPlayer"}:
        return bool(re.search(
            r"\b(attack|kill|fight|hunt)\b|攻击|打死|杀|猎杀",
            message,
        ))
    if name == "breakNearbyBlocks":
        if re.search(r"\b(plant|farm|sow|seed|wheat)\b|种|播种|耕|小麦|种子|田", message):
            return False
        return bool(re.search(
            r"\b(break|destroy|clear|remove|mine|dig)\b|破坏|打掉|清理|挖|采",
            message,
        ))
    if name == "clearFurnace":
        return bool(re.search(r"\bclear\s+(the\s+)?furnace\b|清空.*熔炉|清理.*熔炉", message))
    if name == "digDown":
        return bool(re.search(
            r"\b(dig\s+down|mine\s+down|tunnel\s+down)\b|向下挖|往下挖|挖下去|垂直挖",
            message,
        ))
    return False


def world_change_command_has_intrinsic_effect(command_name: str) -> bool:
    """Return True when a successful result line is enough world-change evidence."""
    return command_name.lstrip("!") in WORLD_CHANGE_EVIDENCE_FALLBACK_COMMANDS
