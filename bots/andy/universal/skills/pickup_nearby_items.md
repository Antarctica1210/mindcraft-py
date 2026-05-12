---
name: pickup_nearby_items
description: Pick up nearby dropped items.
scope: bot
triggers: [pick up items, pickup nearby items, collect drops, grab dropped items]
requires: []
confidence: 0.93
success_count: 0
fail_count: 0
tags: [items, routine]
---

## 前置检查

- The player is asking for nearby dropped items, not a broad resource-gathering task.

## 执行步骤

- !pickupNearbyItems(distance=8)

## 失败模式

- timeout |  | Move closer to the dropped items, then retry.
