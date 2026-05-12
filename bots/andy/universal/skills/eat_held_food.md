---
name: eat_held_food
description: Eat a known food item from inventory when hungry.
scope: bot
triggers: [eat bread, consume bread, eat food]
requires: [food < 18, inventory has bread]
confidence: 0.91
success_count: 0
fail_count: 0
tags: [food, safety]
---

## 前置检查

- The bot has bread and hunger is below full.

## 执行步骤

- !consume(item_name="bread")

## 失败模式

- don't have |  | Recheck inventory before trying another food.
- missing |  | Recheck inventory before trying another food.
