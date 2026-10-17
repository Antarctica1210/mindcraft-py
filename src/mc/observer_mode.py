"""
observer_mode.py — 观察-推断-行动 自主模式
bot 只通过观察玩家行为决定行动，不接受指令
"""
import time
from collections import deque
from dataclasses import dataclass, field

from javascript import On, require

mineflayer = require("mineflayer")
pathfinder = require("mineflayer-pathfinder")
pvp = require("mineflayer-pvp")

MOVEMENT = pathfinder.MovementGoalNear
COMBAT_INTERESTS = {
    "zombie": 0.6, "drowned": 0.6, "husk": 0.5,
    "skeleton": 0.8, "stray": 0.8,
    "spider": 0.7, "cave_spider": 0.9,
    "creeper": 1.0,          # 苦力怕最高优先级
    "witch": 0.7,
    "enderman": 0.3,
    "pillager": 0.7, "vindicator": 0.8, "evoker": 0.9,
    "phantom": 0.6,
    "baby_zombie": 0.9,
}
HEALTH_MONITOR_WINDOW = 3   # 观察最近 3 秒的血量变化
ESCORT_DISTANCE = 8         # 跟随距离
INTERCEPT_DISTANCE = 15     # 最大拦截距离
ATTACK_THRESHOLD = 0.5      # 血量骤降阈值（每秒损失超过 0.5 心 = 被攻击）


@dataclass
class PlayerState:
    """观察到的玩家状态快照"""
    username: str
    health: float = 20.0
    position: tuple = (0, 0, 0)
    is_attacking: bool = False
    last_damage_time: float = 0.0
    health_history: deque = field(default_factory=lambda: deque(maxlen=30))
    # 血量历史：[(timestamp, health), ...]


class ObserverAssistant:
    """观察型助手 bot —— 不接收命令，纯粹观察主人行为然后自主行动"""

    def __init__(self, host: str, port: int, master_username: str):
        self.master_name = master_username
        self.master = PlayerState(username=master_username)

        self.bot = mineflayer.createBot({
            "host": host,
            "port": port,
            "username": "ObserverBot",
        })
        self.bot.loadPlugin(pathfinder.pathfinder)
        self.bot.loadPlugin(pvp.plugin)

        self.mode = "idle"          # idle | following | defending | helping
        self.threat_target = None   # 当前威胁目标
        self.last_eval_time = time.time()
        self.idle_anchor = None     # 空闲时停留在哪

        self._register_events()

    # ── 事件注册 ────────────────────────────────────────────

    def _register_events(self):
        bot = self.bot

        @On(bot, "spawn")
        def on_spawn(this):
            print("[Observer] 已上线，开始观察主人...")
            self.idle_anchor = bot.entity.position

        @On(bot, "entityHurt")
        def on_entity_hurt(this, entity):
            """核心事件：有实体受伤"""
            self._on_entity_hurt(entity)

        @On(bot, "entityDead")
        def on_entity_dead(this, entity):
            """威胁消除"""
            if entity == self.threat_target or (
                self.threat_target and entity.username == self.threat_target
            ):
                self.threat_target = None
                self.mode = "following"
                print("[Observer] 威胁已消除，回到跟随模式")

        @On(bot, "physicsTick")
        def on_tick(this):
            """每 tick 评估一次（20次/秒）"""
            now = time.time()
            if now - self.last_eval_time < 0.15:  # 每 3 tick 评估一次即可
                return
            self.last_eval_time = now
            self._evaluate()

        @On(bot, "playerCollect")
        def on_collect(this, collector, item):
            """主人捡东西 → 附近的掉落物可能值得关注，但不触发主逻辑"""
            pass  # 可扩展：主人在捡掉落物说明刚打死了什么

    # ── 推断逻辑 ────────────────────────────────────────────

    def _on_entity_hurt(self, entity):
        """当任意实体受伤时，判断是否主人被攻击"""
        # 判断受伤的是不是主人
        if entity.username == self.master_name or (
            hasattr(entity, "name") and entity.name == self.master_name
        ):
            now = time.time()
            self.master.last_damage_time = now
            self.master.health = getattr(entity, "health", self.master.health)
            self.master.health_history.append((now, self.master.health))

            # 查找攻击者：最近的敌对实体
            attacker = self._find_likely_attacker(entity)
            if attacker and attacker != self.threat_target:
                self.threat_target = attacker
                self.mode = "defending"
                name = attacker.displayName or attacker.name
                print(f"[Observer] ⚔️ 主人被攻击！推断攻击者: {name}")

    def _find_likely_attacker(self, victim_entity):
        """推断谁在攻击受害者"""
        victim_pos = victim_entity.position
        closest = None
        closest_dist = float("inf")

        for ent_id, entity in self.bot.entities.items():
            if entity.type != "hostile" and entity.type != "mob":
                continue
            if not entity.position:
                continue

            dist = entity.position.distanceTo(victim_pos)
            # 攻击者通常在受害者 3 格以内
            if dist < 3.5 and dist < closest_dist:
                # 检查实体的攻击目标元数据
                # 部分 mob 会暴露 attackTarget entity id
                # entity metadata may expose attackTarget entity id
                _metadata = getattr(entity, "metadata", [])

                closest = entity
                closest_dist = dist

        return closest

    def _estimate_health_drop_rate(self) -> float:
        """计算主人每秒血量下降速率（心/秒）"""
        history = self.master.health_history
        if len(history) < 2:
            return 0.0
        t0, h0 = history[0]
        t1, h1 = history[-1]
        dt = t1 - t0
        if dt < 0.1:
            return 0.0
        return (h1 - h0) / dt  # 负值表示在掉血

    # ── 主评估循环 ─────────────────────────────────────────

    def _evaluate(self):
        """每 3 tick 评估一次：我要做什么？"""
        bot = self.bot

        # 1. 检查主人状态
        master_entity = None
        if self.master_name in bot.players:
            player = bot.players[self.master_name]
            if player and player.entity:
                master_entity = player.entity
                self.master.health = getattr(master_entity, "health", 20.0)
                self.master.position = (
                    master_entity.position.x,
                    master_entity.position.y,
                    master_entity.position.z,
                )

        # 2. 威胁判断：血量在下降 + 附近有敌对生物
        health_drop = self._estimate_health_drop_rate()

        if health_drop < -ATTACK_THRESHOLD:
            # 主人在持续掉血 → 大概率在被攻击
            if master_entity:
                attacker = self._find_likely_attacker(master_entity)
                if attacker:
                    self.mode = "defending"
                    self.threat_target = attacker
                    name = attacker.displayName or attacker.name
                    print(f"[Observer] 主人掉血 ({health_drop:.1f}/s)，推断: {name}")

        # 3. 检查主人是否在攻击什么（辅助输出）
        if master_entity and self.mode != "defending":
            nearest_hostile = self._nearest_hostile_to(master_entity.position)
            if nearest_hostile:
                dist_to_master = nearest_hostile.position.distanceTo(master_entity.position)
                if dist_to_master < 5:
                    self.threat_target = nearest_hostile
                    self.mode = "defending"
                    name = nearest_hostile.displayName or nearest_hostile.name
                    print(f"[Observer] 附近有敌对生物 {name}，协助战斗")

        # 4. 执行当前模式
        if self.mode == "defending":
            self._defend()
        elif self.mode == "following":
            self._follow_master(master_entity)
        elif self.mode == "idle":
            self._idle_near_master(master_entity)
        elif self.mode == "helping":
            self._assist(master_entity)

    # ── 行动执行 ────────────────────────────────────────────

    def _defend(self):
        """防御模式：拦截并消灭威胁"""
        if not self.threat_target:
            self.mode = "following"
            return

        bot = self.bot
        target = self.threat_target

        if not target or not target.isValid:
            self.threat_target = None
            self.mode = "following"
            return

        target_pos = target.position
        dist = bot.entity.position.distanceTo(target_pos)

        if hasattr(bot, "pvp") and bot.pvp:
            # 使用 pvp 插件的智能战斗（自动走位、格挡、暴击）
            bot.pvp.attack(target)
        elif dist < 3.5:
            # 简单攻击
            bot.attack(target)
        else:
            # 移动到目标
            bot.pathfinder.setGoal(
                MOVEMENT(target_pos.x, target_pos.y, target_pos.z, 3)
            )

    def _follow_master(self, master_entity):
        """跟随主人"""
        if not master_entity:
            return
        bot = self.bot
        target_pos = master_entity.position
        dist = bot.entity.position.distanceTo(target_pos)
        if dist > ESCORT_DISTANCE:
            bot.pathfinder.setGoal(
                MOVEMENT(target_pos.x, target_pos.y, target_pos.z, 4)
            )

    def _idle_near_master(self, master_entity):
        """在主人附近闲逛，保持不远不近"""
        if not master_entity:
            return
        bot = self.bot
        target_pos = master_entity.position
        dist = bot.entity.position.distanceTo(target_pos)
        if dist > ESCORT_DISTANCE * 1.5:
            self.mode = "following"
        # 否则不动，节省路径计算

    def _assist(self, master_entity):
        """辅助模式：主人做什么就帮忙做什么"""
        # 可扩展：主人挖矿 → 挖旁边的；主人合成 → 递材料
        pass

    # ── 工具方法 ────────────────────────────────────────────

    def _nearest_hostile_to(self, position, max_dist=INTERCEPT_DISTANCE):
        """找最近的敌对生物"""
        closest = None
        closest_dist = max_dist + 1
        for ent_id, entity in self.bot.entities.items():
            if entity.type != "hostile":
                continue
            if not entity.position:
                continue
            dist = entity.position.distanceTo(position)
            name = (entity.displayName or entity.name or "").lower()
            interest = COMBAT_INTERESTS.get(name, 0.5)
            # 加权距离：高优先级敌人看起来更近
            weighted = dist / max(interest, 0.1)
            if weighted < closest_dist:
                closest_dist = weighted
                closest = entity
        return closest

    def shutdown(self):
        self.bot.quit()
