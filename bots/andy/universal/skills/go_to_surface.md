---
name: go_to_surface
description: Return to the surface.
scope: bot
triggers: [go to surface, return to surface, get to surface, surface]
requires: [pos.y < 63]
confidence: 0.92
success_count: 0
fail_count: 0
tags: [movement, safety]
---

## 前置检查

- The bot is below typical surface height.

## 执行步骤

- !goToSurface()

## 失败模式

- path |  | Ask the planner to choose a safer route or dig upward carefully.
- timeout |  | Retry once after checking nearby hazards.
