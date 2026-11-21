"""unos mech bot: survive the zone, grab a laser, fight from range with jinking, retreat to heal."""
import math
import os
import random
import sys
import time

import mechfight as mf
from world import AStar, World

HERE = os.path.dirname(os.path.abspath(__file__))
PI = math.pi
EXPLORE = os.environ.get("UNOS_EXPLORE", "0") == "1"
TAU = 2 * PI

UPGRADE_ORDER = [
    "armor", "laser_charge", "plating", "laser_damage", "nanites", "gun_rate", "gun_damage",
    "gyros", "servos", "jets", "lightning_efficiency", "capacitor", "reactor", "gun_mag",
    "sensors", "gun_velocity", "lightning_range", "lightning_chain", "laser_pierce",
    "mortar_reload", "mortar_velocity", "mortar_splash",
]


def wrap(a):
    return (a + PI) % TAU - PI


def hyp(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def norm(x, y):
    L = math.hypot(x, y)
    return (x / L, y / L) if L > 1e-9 else (0.0, 0.0)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Bot:
    name = "unos"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.rng = random.Random(hello.get("seed", 1))
        mdir = hello.get("map", {}).get("dir")
        arena = mdir if mdir and os.path.isdir(os.path.join(mdir, "v2")) else None
        self.w = World(arena, os.path.join(HERE, "grid.bin"))
        self.path = []
        self.path_goal = None
        self.path_t = -99
        self.astar = None
        self.astar_goal = None
        self.hist = []
        self.stuck_until = -1
        self.stuck_count = 0
        self.stuck_dir = None
        self.jink = 1
        self.jink_until = 0
        self.boosting = False
        self.threat = None          # (t, bearing, by)
        self.enemy_mem = {}         # id -> (t, pos, vel, hp)
        self.loot_mem = {}          # pickup id -> (t, kind, pos)
        self.crate_mem = {}         # crate id -> (t, pos)
        self.retreat_until = -1
        self.cover = None
        self.cover_t = -99
        self.cover_born = -99
        self.last_hit_t = -99
        self.mode = "start"
        self.prev_move = (0.0, 0.0)
        self.t0 = 0
        self.blacklist = {}
        self.dmg_in = []
        self.dmg_out = []
        self.peek_until = -1
        self.aux_id = None
        self.aux_since = (None, 0)

    # ------------------------------------------------------------------ helpers
    def zone_now(self, z, t):
        ss, se = z["shrink_start"], z["shrink_end"]
        c, r, nc, nr = z["center"], z["radius"], z["next_center"], z["next_radius"]
        if t <= ss or se <= ss:
            return c, r
        if t >= se:
            return nc, nr
        s = (t - ss) / (se - ss)
        return [c[0] + (nc[0] - c[0]) * s, c[1] + (nc[1] - c[1]) * s], r + (nr - r) * s

    def in_safe(self, x, y, margin=0.0):
        nc, nr = self.z["next_center"], self.z["next_radius"]
        return hyp(x, y, nc[0], nc[1]) <= max(0.0, nr - margin)

    def goal_allowed(self, x, y):
        """Can we go to (x, y) for loot without risking the zone?"""
        z, t = self.z, self.t
        cc, cr = self.zone_now(z, t)
        if hyp(x, y, cc[0], cc[1]) > cr - 15:
            return False
        if self.in_safe(x, y, 12):
            return True
        nc, nr = z["next_center"], z["next_radius"]
        back = hyp(x, y, nc[0], nc[1]) - (nr - 15)
        slack = z["shrink_start"] - t
        return back / 10.0 * 1.6 + hyp(x, y, self.x, self.y) / 10.0 < slack - 8

    def eye(self):
        return (self.x, self.y, self.z_ + self.me["stats"].get("eye_height", 8))

    def muzzle(self):
        return (self.x, self.y, self.z_ + self.me["stats"].get("muzzle_height", 7))

    def can_see(self, p, h=4.5):
        e = self.eye()
        return self.w.los(e, (p[0], p[1], p[2] + h)) or self.w.los(e, (p[0], p[1], p[2] + h * 1.8))

    # ------------------------------------------------------------------ navigation
    def plan_step(self):
        if self.astar is None:
            return
        t0 = time.perf_counter()
        while not self.astar.done and time.perf_counter() - self.tick_start < 0.010:
            self.astar.step(150)
        if self.astar.done:
            p = self.astar.path
            if p:
                self.path = [self.w.c4(k) for k in p]
                self.path_goal = self.astar_goal
                self.path_t = self.t
            else:
                self.path = []
                self.path_goal = self.astar_goal
                self.path_t = self.t
            self.astar = None
        _ = t0

    def nav_dir(self, gx, gy):
        """Direction to walk toward (gx, gy), using A* when the straight line is blocked."""
        w = self.w
        x, y = self.x, self.y
        if hyp(x, y, gx, gy) < 1.5:
            return (0.0, 0.0)
        if hyp(x, y, gx, gy) < 60 and w.line_walk((x, y), (gx, gy)):
            self.path = []
            return norm(gx - x, gy - y)
        need = (not self.path or self.path_goal is None or hyp(gx, gy, *self.path_goal) > 10
                or self.t - self.path_t > 6)
        if need and (self.astar is None or hyp(gx, gy, *self.astar_goal) > 10):
            s = w.nearest_walk(x, y, 10)
            g = w.nearest_walk(gx, gy, 15)
            if s >= 0 and g >= 0:
                self.astar = AStar(w, s, g)
                self.astar_goal = (gx, gy)
        self.plan_step()
        if self.path:
            while len(self.path) > 1 and hyp(x, y, *self.path[0]) < 4.5:
                self.path.pop(0)
            tgt = self.path[0]
            for k in range(min(len(self.path) - 1, 7), 0, -1):
                if w.line_walk((x, y), self.path[k]):
                    tgt = self.path[k]
                    break
            return norm(tgt[0] - x, tgt[1] - y)
        return norm(gx - x, gy - y)

    # ------------------------------------------------------------------ main
    def act(self, obs):
        self.tick_start = time.perf_counter()
        me = obs["self"]
        self.me = me
        self.t = t = obs["t"]
        self.x, self.y, self.z_ = me["pos"]
        self.z = obs["zone"]
        act = {"tick": obs["tick"]}
        hpf = me["hp"] / max(1.0, me["max_hp"])

        # ---- upgrades
        off = obs.get("offer")
        if off:
            ch = off["choices"]
            best = min(range(len(ch)), key=lambda i: UPGRADE_ORDER.index(ch[i]["id"]) if ch[i]["id"] in UPGRADE_ORDER else 99)
            act["pick"] = best

        # ---- events / memory
        for e in obs.get("events", []):
            ty = e.get("type")
            if ty == "damaged" and e.get("weapon") not in ("zone", None) and e.get("bearing") is not None:
                self.threat = (t, e["bearing"], e.get("by"))
                self.last_hit_t = t
                self.dmg_in.append((t, e.get("amount", 0)))
            elif ty == "hit" and isinstance(e.get("target"), (int, float)) and e["target"] < 16:
                self.dmg_out.append((t, e.get("amount", 0)))
        while self.dmg_in and t - self.dmg_in[0][0] > 4:
            self.dmg_in.pop(0)
        while self.dmg_out and t - self.dmg_out[0][0] > 4:
            self.dmg_out.pop(0)
        din = sum(a for _, a in self.dmg_in) / 4.0
        dout = sum(a for _, a in self.dmg_out) / 4.0
        enemies = []
        npcs = []
        for m in obs.get("mechs", []):
            if m["kind"] == "mech":
                m["_d"] = hyp(self.x, self.y, m["pos"][0], m["pos"][1])
                m["_vis"] = self.can_see(m["pos"])
                enemies.append(m)
                self.enemy_mem[m["id"]] = (t, m["pos"], m["vel"], m.get("hp", 1400))
            else:
                m["_d"] = hyp(self.x, self.y, m["pos"][0], m["pos"][1])
                npcs.append(m)
        for p in obs.get("pickups", []):
            self.loot_mem[p["id"]] = (t, p["kind"], p["pos"])
        seen_p = {p["id"] for p in obs.get("pickups", [])}
        for pid, (tt, kind, pos) in list(self.loot_mem.items()):
            if t - tt > 90 or (pid not in seen_p and hyp(self.x, self.y, pos[0], pos[1]) < 30):
                del self.loot_mem[pid]
        for c in obs.get("crates", []):
            self.crate_mem[c["id"]] = (t, c["pos"])
        seen_c = {c["id"] for c in obs.get("crates", [])}
        for cid, (tt, pos) in list(self.crate_mem.items()):
            if t - tt > 90 or (cid not in seen_c and hyp(self.x, self.y, pos[0], pos[1]) < 35):
                del self.crate_mem[cid]

        weapons = me["weapons"]
        kinds = [w["kind"] if w else None for w in weapons]
        has_laser = "laser" in kinds
        has_light = "lightning" in kinds

        # ---- pick up weapons in reach
        reach = self.cfg["loot"]["pickup_radius"] - 0.3
        for p in obs.get("pickups", []):
            if hyp(self.x, self.y, p["pos"][0], p["pos"][1]) > reach or abs(p["pos"][2] - self.z_) > 3.8:
                continue
            k = p["kind"]
            if k == "laser" and not has_laser:
                if kinds[1] is None:
                    act["take"] = p["id"]
                elif not (weapons[1] and weapons[1].get("charge", 0) > 0):
                    act["take"] = p["id"]; act["drop"] = 1
                break
            if k == "lightning" and not has_light and kinds[1] is None:
                act["take"] = p["id"]
                break

        # ---- zone situation
        z = self.z
        nc, nr = z["next_center"], z["next_radius"]
        cc, cr = self.zone_now(z, t)
        d_next = hyp(self.x, self.y, nc[0], nc[1])
        margin = min(30.0, 0.3 * nr)
        d_out = d_next - (nr - margin)
        speed = me["stats"].get("walk_speed", 11.7)
        must_zone = False
        if not me["in_zone"] or hyp(self.x, self.y, cc[0], cc[1]) > cr - 8:
            must_zone = True
        elif d_out > 0:
            if t >= z["shrink_start"] - 4 or t + d_out / speed * 1.7 + 12 > z["shrink_start"]:
                must_zone = True
        final = nr <= 30
        hold_r = max(0.0, nr - margin) * (0.15 if final else 0.45)
        if d_next > hold_r:
            hx = nc[0] + (self.x - nc[0]) * hold_r / max(d_next, 1e-6)
            hy = nc[1] + (self.y - nc[1]) * hold_r / max(d_next, 1e-6)
        else:
            hx, hy = self.x, self.y
        if final and nr <= 1:
            hx, hy = nc[0], nc[1]

        # ---- choose target
        vis_enemies = [m for m in enemies if m["_vis"]]
        target = None
        if vis_enemies:
            def score(m):
                s = m["_d"] / 60.0 + m.get("hp", 1400) / 350.0
                if self.threat and self.threat[2] == m["id"] and t - self.threat[0] < 3:
                    s -= 2.0
                return s
            target = min(vis_enemies, key=score)
        close_threats = [m for m in vis_enemies if m["_d"] < 160]
        n_threat = len(close_threats)

        # ---- fight / flight
        retreat = False
        if target is not None:
            finishing = target.get("hp", 1400) < 250 and target["_d"] < 140
            if hpf < 0.35 and not finishing:
                retreat = True
            elif n_threat >= 3:
                retreat = True
            elif n_threat >= 2 and (hpf < 0.9 or not finishing):
                retreat = True
        if target is not None and not retreat:
            # losing the damage race: time-to-die vs time-to-kill
            ttd = me["hp"] / max(din, 1.0)
            ttk = target.get("hp", 1400) / max(dout, 1.0)
            if din > 60 and ttd < ttk * 0.8 and hpf < 0.75:
                retreat = True
        if retreat:
            self.retreat_until = t + 3.0
        recently_hit = t - self.last_hit_t < 4.0
        if not target and recently_hit and hpf < 0.5:
            self.retreat_until = max(self.retreat_until, t + 1.0)
        healing = (t < self.retreat_until) or (hpf < 0.6 and target is None and t - self.last_hit_t < 12)

        # ---- movement
        mv = (0.0, 0.0)
        boost = False
        jump = False
        mode = "hold"
        if must_zone:
            mode = "zone"
            gx, gy = (hx, hy) if d_out > 0 or not me["in_zone"] else (nc[0], nc[1])
            if d_out > 0:
                gx = nc[0] + (self.x - nc[0]) * (nr - margin) * 0.85 / max(d_next, 1e-6)
                gy = nc[1] + (self.y - nc[1]) * (nr - margin) * 0.85 / max(d_next, 1e-6)
            mv = self.nav_dir(gx, gy)
            urgent = (not me["in_zone"]) or t > z["shrink_start"] - 2
            boost = urgent and me["energy"] > 15
        elif healing and (vis_enemies or (self.threat and t - self.threat[0] < 4)):
            mode = "retreat"
            mv = self.retreat_move(vis_enemies)
            boost = me["energy"] > 25
        elif target is not None and self.should_duck(weapons, target):
            mode = "duck"
            mv = self.retreat_move(vis_enemies)
            boost = me["energy"] > 25
        elif target is not None:
            mode = "fight"
            mv, boost = self.fight_move(target, has_laser, has_light, hpf)
        else:
            goal, mode = self.pick_goal(obs, npcs, has_laser, has_light, hpf, (hx, hy))
            if goal is not None:
                mv = self.nav_dir(goal[0], goal[1])
            if mode == "hold" and hyp(self.x, self.y, hx, hy) < 6:
                mv = (0.0, 0.0)
            elif mode == "hold":
                mv = self.nav_dir(hx, hy)

        # keep away from zone edge in fights
        if mode in ("fight", "retreat", "duck"):
            dc = hyp(self.x, self.y, cc[0], cc[1])
            if dc > cr - 25 or (t > z["shrink_start"] - 3 and d_out > 0):
                zx, zy = norm(nc[0] - self.x, nc[1] - self.y)
                mv = norm(mv[0] + zx * 1.6, mv[1] + zy * 1.6)

        # boosting in the air is a jet: only allow it on the ground (unless unsticking)
        if not me["grounded"] or self.z_ - self.w.surface(self.x, self.y) > 1.5:
            boost = False
        # ---- stuck handling
        mv, j2, b2 = self.unstick(mv)
        jump = jump or j2
        boost = boost or b2

        act["move"] = [round(mv[0], 3), round(mv[1], 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)

        # ---- aim and fire
        aim, fire, reload = self.aim_fire(obs, target, npcs, mode, mv)
        act["aim"] = [round(aim[0], 4), round(aim[1], 4)]
        act["fire"] = fire
        if reload:
            act["reload"] = True
        self.mode = mode
        self.prev_move = mv
        mf.draw.label(f"{mode} hp{me['hp']:.0f} c{me['cubes']} {''.join(k[0] if k else '-' for k in kinds)}")
        return act

    # ------------------------------------------------------------------ behaviours
    def pick_goal(self, obs, npcs, has_laser, has_light, hpf, hold):
        x, y = self.x, self.y
        me = self.me
        kinds = [w["kind"] if w else None for w in me["weapons"]]
        best, bd, bmode = None, 1e9, "hold"
        # supply drops
        for d in obs.get("drops", []):
            p = d["pos"]
            dist = hyp(x, y, p[0], p[1])
            if dist > 150 or not self.in_safe(p[0], p[1], 5):
                continue
            if d["lands_in"] > dist / 11.0 + 6:
                continue
            others = [m for m in obs.get("mechs", []) if m["kind"] == "mech" and hyp(m["pos"][0], m["pos"][1], p[0], p[1]) < 60]
            if others:
                continue
            if d.get("holder") not in (None, me["id"]):
                continue
            score = dist * 0.6
            if score < bd:
                best, bd, bmode = (p[0], p[1]), score, "drop"
        # weapons
        for pid, (tt, kind, p) in self.loot_mem.items():
            want = (kind == "laser" and not has_laser) or (kind == "lightning" and not has_light and kinds[1] is None)
            if not want:
                continue
            dist = hyp(x, y, p[0], p[1])
            if dist > 140 or not self.goal_allowed(p[0], p[1]):
                continue
            score = dist * (0.5 if kind == "laser" else 0.9)
            if score < bd:
                best, bd, bmode = (p[0], p[1]), score, "loot"
        # cubes
        for c in obs.get("cubes", []):
            p = c["pos"]
            dist = hyp(x, y, p[0], p[1])
            if dist > 80 or not self.goal_allowed(p[0], p[1]):
                continue
            if dist * 0.8 < bd:
                best, bd, bmode = (p[0], p[1]), dist * 0.8, "cube"
        # crates (shoot from range; walk closer only if far)
        if hpf > 0.4:
            for cid, (tt, p) in self.crate_mem.items():
                if self.blacklist.get(cid, -99) > self.t:
                    continue
                dist = hyp(x, y, p[0], p[1])
                if dist > 110 or not self.goal_allowed(p[0], p[1]):
                    continue
                sc = dist * 1.2 + 10
                if sc < bd:
                    if dist < 40 and self.w.los(self.muzzle(), (p[0], p[1], p[2] + 1.2)):
                        best, bd, bmode = (x, y), sc, "crate"
                    else:
                        best, bd, bmode = (p[0], p[1]), sc, "crate"
                    self.aux_id = cid
        # NPC drones for cubes
        if hpf > 0.6 and self.t < 260:
            for n in npcs:
                p = n["pos"]
                if n["_d"] > 90 or not self.goal_allowed(p[0], p[1]):
                    continue
                if self.blacklist.get(n["id"], -99) > self.t:
                    continue
                sc = n["_d"] * 1.4 + 20
                if sc < bd:
                    if n["_d"] < 55 and self.w.los(self.muzzle(), (p[0], p[1], p[2] + 1.8)):
                        best, bd, bmode = (x, y), sc, "npc"
                    else:
                        best, bd, bmode = (p[0], p[1]), sc, "npc"
                    self.aux_id = n["id"]
        if best is None and EXPLORE and not has_laser and self.t < 260 and hpf > 0.5:
            ex = self.explore_goal()
            if ex is not None:
                return ex, "explore"
        if best is None:
            return hold, "hold"
        if bmode in ("npc", "crate"):
            if self.aux_since[0] != self.aux_id:
                self.aux_since = (self.aux_id, self.t)
            elif self.t - self.aux_since[1] > 14:
                self.blacklist[self.aux_id] = self.t + 60
                self.aux_since = (None, 0)
        return best, bmode

    def should_duck(self, weapons, target):
        """Gun reloading and nothing else ready: break line of sight until it is back."""
        g = next((w for w in weapons if w and w["kind"] == "gun"), None)
        other_ready = any(w and w["kind"] in ("laser", "lightning") and w.get("cooldown", 0) < 0.3 for w in weapons)
        if other_ready:
            return False
        if g is None:
            return False
        reloading = g.get("reload", 0) > 0.6 or g.get("ammo", 12) == 0
        if not (reloading and target["_d"] > 30):
            return False
        threats = [target["pos"]]
        c = self.cover
        if c is None or not self.hidden(c, threats):
            if self.t - self.cover_t < 0.5:
                return False
            c = self.find_cover(threats, maxr=18)
            self.cover, self.cover_t, self.cover_born = c, self.t, self.t
        return c is not None and hyp(self.x, self.y, c[0], c[1]) < 18

    def explore_goal(self):
        g = getattr(self, "_explore", None)
        if g is not None and (hyp(self.x, self.y, g[0], g[1]) < 12 or self.t - g[2] > 25 or not self.goal_allowed(g[0], g[1])):
            g = None
        if g is None:
            nc, nr = self.z["next_center"], self.z["next_radius"]
            for _ in range(30):
                a = self.rng.uniform(0, TAU)
                r = self.rng.uniform(70, 150)
                x, y = self.x + math.cos(a) * r, self.y + math.sin(a) * r
                if self.w.walkable(x, y) and self.goal_allowed(x, y) and hyp(x, y, nc[0], nc[1]) < nr * 0.85:
                    g = (x, y, self.t)
                    break
            self._explore = g
        return (g[0], g[1]) if g else None

    def fight_move(self, target, has_laser, has_light, hpf):
        t = self.t
        tx, ty = target["pos"][0], target["pos"][1]
        d = target["_d"]
        ux, uy = norm(tx - self.x, ty - self.y)
        if t > self.jink_until:
            self.jink = -self.jink if self.rng.random() < 0.75 else self.jink
            self.jink_until = t + self.rng.uniform(0.45, 1.25)
        px, py = -uy * self.jink, ux * self.jink
        if has_laser:
            lo, hi = 70, 170
        else:
            lo, hi = 45, 110
        if has_light and hpf > 0.6 and target.get("hp", 1400) < self.me["hp"]:
            lo, hi = 15, 35
        radial = 0.0
        if d > hi:
            radial = 0.7
        elif d < lo:
            radial = -0.7
        mx, my = px + ux * radial, py + uy * radial
        mx, my = norm(mx, my)
        # do not run into walls
        if not self.w.walkable(self.x + mx * 6, self.y + my * 6):
            mx2, my2 = norm(-px + ux * radial, -py + uy * radial)
            if self.w.walkable(self.x + mx2 * 6, self.y + my2 * 6):
                self.jink = -self.jink
                mx, my = mx2, my2
        e = self.me["energy"]
        if e > 70:
            self.boosting = True
        elif e < 30:
            self.boosting = False
        return (mx, my), self.boosting

    def retreat_move(self, vis_enemies):
        t = self.t
        threats = [m["pos"] for m in vis_enemies[:3]]
        if not threats and self.threat:
            b = self.threat[1]
            threats = [(self.x + math.cos(b) * 120, self.y + math.sin(b) * 120, self.z_)]
        c = self.cover
        if c is not None and (t - self.cover_t > 0.3):
            if t - getattr(self, "cover_born", t) > 12 or not self.hidden(c, threats) or hyp(self.x, self.y, c[0], c[1]) > 60:
                c = None
            else:
                self.cover_t = t
        if c is None:
            c = self.find_cover(threats)
            self.cover, self.cover_t = c, t
            self.cover_born = t
        if c is not None:
            if hyp(self.x, self.y, c[0], c[1]) < 2.5:
                return (0.0, 0.0)
            return self.nav_dir(c[0], c[1])
        # run away from the threat centroid, staying in zone
        ax = sum(p[0] for p in threats) / max(1, len(threats))
        ay = sum(p[1] for p in threats) / max(1, len(threats))
        mx, my = norm(self.x - ax, self.y - ay)
        nc = self.z["next_center"]
        zx, zy = norm(nc[0] - self.x, nc[1] - self.y)
        return norm(mx + zx * 0.5, my + zy * 0.5)

    def hidden(self, c, threats):
        w = self.w
        gz = w.surface(c[0], c[1])
        for p in threats:
            if w.los((p[0], p[1], p[2] + 8), (c[0], c[1], gz + 5)) or w.los((p[0], p[1], p[2] + 8), (c[0], c[1], gz + 8.5)):
                return False
        return True

    def find_cover(self, threats, maxr=40):
        if not threats:
            return None
        best, bd = None, 1e9
        w = self.w
        for r in [r for r in (8, 14, 22, 32, 44) if r <= maxr]:
            for k in range(16):
                a = k * TAU / 16
                cx, cy = self.x + math.cos(a) * r, self.y + math.sin(a) * r
                if not w.walkable(cx, cy) or not self.in_safe(cx, cy, 5) and not self.goal_allowed(cx, cy):
                    continue
                gz = w.surface(cx, cy)
                hidden = True
                for p in threats:
                    if w.los((p[0], p[1], p[2] + 8), (cx, cy, gz + 5)) or w.los((p[0], p[1], p[2] + 8), (cx, cy, gz + 8.5)):
                        hidden = False
                        break
                if not hidden:
                    continue
                # prefer moving away from threats
                away = sum(hyp(cx, cy, p[0], p[1]) for p in threats) / len(threats)
                sc = r - away * 0.1
                if sc < bd:
                    bd, best = sc, (cx, cy)
            if best is not None and r >= 18:
                break
            if time.perf_counter() - self.tick_start > 0.014:
                break
        return best

    def unstick(self, mv):
        t = self.t
        self.hist.append((t, self.x, self.y, abs(mv[0]) + abs(mv[1]) > 0.5))
        while self.hist and t - self.hist[0][0] > 1.6:
            self.hist.pop(0)
        if t < self.stuck_until:
            d = self.stuck_dir or mv
            # jump, then jet while holding the move
            grounded = self.me["grounded"]
            return d, grounded, (not grounded) and self.me["energy"] > 5
        if len(self.hist) >= 14 and all(h[3] for h in self.hist):
            t0, x0, y0, _ = self.hist[0]
            if hyp(self.x, self.y, x0, y0) < 2.5 and t - t0 > 1.3:
                self.stuck_count += 1
                self.stuck_until = t + 1.4
                if self.stuck_count % 3 == 0:
                    a = self.rng.uniform(0, TAU)
                    self.stuck_dir = (math.cos(a), math.sin(a))
                else:
                    self.stuck_dir = mv
                self.path = []
                self.path_t = -99
                self.hist.clear()
                return self.stuck_dir, self.me["grounded"], False
        return mv, False, False

    # ------------------------------------------------------------------ aiming
    def lead(self, pos, vel, spd, h):
        mx, my, mz = self.muzzle()
        tx, ty, tz = pos[0], pos[1], pos[2] + h
        tt = 0.0
        for _ in range(4):
            px, py, pz = tx + vel[0] * tt, ty + vel[1] * tt, tz + vel[2] * tt * 0.5
            d = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (pz - mz) ** 2)
            tt = max(0.0, d - 3.0) / spd + 0.05
        px, py, pz = tx + vel[0] * tt, ty + vel[1] * tt, tz + vel[2] * tt * 0.5
        yaw = math.atan2(py - my, px - mx)
        pitch = math.atan2(pz - mz, math.hypot(px - mx, py - my))
        return yaw, pitch, (px, py, pz)

    def direct(self, pos, vel, h, dt=0.05):
        mx, my, mz = self.muzzle()
        px, py, pz = pos[0] + vel[0] * dt, pos[1] + vel[1] * dt, pos[2] + h + vel[2] * dt
        return math.atan2(py - my, px - mx), math.atan2(pz - mz, math.hypot(px - mx, py - my)), (px, py, pz)

    def aim_fire(self, obs, target, npcs, mode, mv):
        me = self.me
        t = self.t
        cur = me["aim"]
        weapons = me["weapons"]
        fire = [False, False]
        reload = False
        aim = None
        tgt = target
        tgt_h = 4.5 * (1 + 0.05 * (tgt.get("cubes", 0) if tgt else 0))
        aux = None  # crate or npc target (gun only)
        recent_mech = any(t - tt < 6 and hyp(self.x, self.y, p[0], p[1]) < 220 for tt, p, v, hp in self.enemy_mem.values())
        if tgt is None and not recent_mech and mode in ("crate", "npc", "hold", "cube", "loot", "drop", "explore"):
            # shoot nearby npcs / crates when not fighting mechs
            cands = []
            if mode == "npc" or self.me["hp"] / self.me["max_hp"] > 0.6:
                for n in npcs:
                    if n["_d"] < 70 and self.w.los(self.muzzle(), (n["pos"][0], n["pos"][1], n["pos"][2] + 1.8)):
                        cands.append((n["_d"], n["pos"], n["vel"], 1.8))
            for c in obs.get("crates", []):
                p = c["pos"]
                dd = hyp(self.x, self.y, p[0], p[1])
                if dd < 45 and self.w.los(self.muzzle(), (p[0], p[1], p[2] + 1.2)):
                    cands.append((dd + 10, p, (0, 0, 0), 1.2))
            if cands:
                aux = min(cands, key=lambda c: c[0])

        gun_i = next((i for i, w in enumerate(weapons) if w and w["kind"] == "gun"), None)
        las_i = next((i for i, w in enumerate(weapons) if w and w["kind"] == "laser"), None)
        lit_i = next((i for i, w in enumerate(weapons) if w and w["kind"] == "lightning"), None)

        if tgt is not None:
            pos, vel = tgt["pos"], tgt["vel"]
            dist = tgt["_d"]
            gun_spd = weapons[gun_i]["stats"]["speed"] if gun_i is not None else 252
            gy, gp, gpt = self.lead(pos, vel, gun_spd, tgt_h)
            aim = (gy, gp)
            if las_i is not None:
                L = weapons[las_i]
                full = L["stats"].get("charge", 0.75)
                ch = L.get("charge", 0) or 0
                lrange = L["stats"].get("range", 280)
                if ch > 0 or (L.get("cooldown", 0) <= 0.05 and dist < lrange * 0.95):
                    if ch >= full - 0.12:
                        dy_, dp_, dpt = self.direct(pos, vel, tgt_h)
                        aim = (dy_, dp_)
                        err = abs(wrap(cur[0] - dy_)) + abs(cur[1] - dp_)
                        tol = math.atan2(1.6, max(dist, 1))
                        if ch >= full - 0.02 and err < tol and self.w.los(self.muzzle(), dpt):
                            fire[las_i] = False  # release
                        else:
                            fire[las_i] = True
                    else:
                        fire[las_i] = True
            if gun_i is not None:
                G = weapons[gun_i]
                grange = G["stats"].get("range", 230)
                err_y = abs(wrap(cur[0] - gy))
                err_p = abs(cur[1] - gp)
                tol_y = math.atan2(2.2, max(dist, 1)) + 0.004
                tol_p = math.atan2(3.0, max(dist, 1)) + 0.004
                if (dist < grange - 5 and err_y < tol_y and err_p < tol_p and (G.get("ammo", 1) > 0)
                        and self.w.los(self.muzzle(), gpt)):
                    fire[gun_i] = True
            if lit_i is not None:
                LT = weapons[lit_i]
                if dist < LT["stats"].get("range", 45) and abs(wrap(cur[0] - math.atan2(pos[1] - self.y, pos[0] - self.x))) < math.radians(LT["stats"].get("cone_deg", 30)) * 0.8 and me["energy"] > 12:
                    fire[lit_i] = True
        elif aux is not None:
            _, pos, vel, h = aux
            gy, gp, gpt = self.lead(pos, vel, 252, h)
            aim = (gy, gp)
            dist = hyp(self.x, self.y, pos[0], pos[1])
            if gun_i is not None:
                G = weapons[gun_i]
                if abs(wrap(cur[0] - gy)) < math.atan2(1.2, max(dist, 1)) + 0.01 and abs(cur[1] - gp) < 0.06 and G.get("ammo", 1) > 0:
                    fire[gun_i] = True
            if lit_i is not None and dist < 40 and me["energy"] > 40:
                if abs(wrap(cur[0] - gy)) < 0.3:
                    fire[lit_i] = True
            if las_i is not None:
                L = weapons[las_i]
                if (L.get("charge", 0) or 0) > 0:
                    fire[las_i] = not ((L.get("charge", 0) or 0) >= L["stats"].get("charge", 0.75) - 0.02)
        else:
            # no target: face threat, or scan
            if las_i is not None and (weapons[las_i].get("charge", 0) or 0) > 0:
                fire[las_i] = True  # keep holding; it auto-fires eventually
            base = None
            if self.threat and t - self.threat[0] < 4:
                base = self.threat[1]
            else:
                best = None
                for eid, (tt, p, v, hp) in self.enemy_mem.items():
                    if t - tt < 8:
                        dd = hyp(self.x, self.y, p[0], p[1])
                        if best is None or dd < best[0]:
                            best = (dd, math.atan2(p[1] - self.y, p[0] - self.x))
                if best:
                    base = best[1]
                else:
                    for s in obs.get("sounds", []):
                        if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "mortar") and s["dist"] != "far":
                            base = s["bearing"]
                            break
            if base is None:
                # full sweep: weapons and crates are only listed while in view
                base = wrap(t * 2.6)
            aim = (base, -0.02)
            if gun_i is not None:
                G = weapons[gun_i]
                if G.get("reload", 0) <= 0 and G.get("ammo", 99) < G["stats"].get("mag", 12) * 0.5:
                    reload = True
        return aim, fire, reload


if __name__ == "__main__":
    mf.run(Bot)
